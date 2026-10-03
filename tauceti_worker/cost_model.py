"""Read-only cost and scaling estimates from Tau Ceti's local agent transcripts.

The analyzer deliberately reports physical quantities separately from prices.  Local logs can
measure wall time and token traffic; converting those into CPU-hours or dollars requires explicit
assumptions about parallelism and the runner/provider price.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
import math
import re
import statistics
from collections import defaultdict
from collections.abc import Iterable
from pathlib import Path

WALL_RE = re.compile(r"Wall time(?::|\s)(?:\s*)([0-9.]+)\s*seconds", re.I)
PROCESS_RE = re.compile(r"(?:session ID|cell ID)\s+([A-Za-z0-9_-]+)", re.I)
CLAIM_RE = re.compile(r'claim\.sh\s+acquire\s+["\']author/([^/"\']+)/')
PR_RE = re.compile(r"pull request\s+#(\d+)", re.I)
OPENED_PR_RE = re.compile(r"(?:Opened PR\s+#|TauCetiProject/TauCeti/pull/)(\d+)", re.I)
LONG_CONTEXT_THRESHOLD = 272_000

# API-equivalent list prices in USD / 1M tokens. These are deliberately visible in JSON output
# and dated: tokens are measured facts, dollars are a conversion at the rate in force when a
# session ran. A price change adds a window; a model-name change adds a model.
AI_PRICES = {
    "gpt-5.5": [
        {
            "effective": "2026-01-01",
            "input": 5.0,
            "cached_input": 0.5,
            "cache_write": 6.25,
            "output": 30.0,
            "long_context_threshold": 272_000,
            "long_input": 10.0,
            "long_cached_input": 1.0,
            "long_cache_write": 12.5,
            "long_output": 45.0,
        }
    ],
    "gpt-5.6-sol": [
        {
            "effective": "2026-07-09",
            "input": 5.0,
            "cached_input": 0.5,
            "cache_write": 6.25,
            "output": 30.0,
            "long_context_threshold": 272_000,
            "long_input": 10.0,
            "long_cached_input": 1.0,
            "long_cache_write": 12.5,
            "long_output": 45.0,
        },
        {
            "effective": "2026-08-21",
            "input": 4.0,
            "cached_input": 0.4,
            "cache_write": 5.0,
            "output": 20.0,
            "long_context_threshold": 272_000,
            "long_input": 8.0,
            "long_cached_input": 0.8,
            "long_cache_write": 10.0,
            "long_output": 30.0,
        },
    ],
    "gpt-5.6-terra": [
        {
            "effective": "2026-07-09",
            "input": 2.5,
            "cached_input": 0.25,
            "cache_write": 3.125,
            "output": 15.0,
            "long_context_threshold": 272_000,
            "long_input": 5.0,
            "long_cached_input": 0.5,
            "long_cache_write": 6.25,
            "long_output": 22.5,
        },
        {
            "effective": "2026-07-30",
            "input": 2.0,
            "cached_input": 0.2,
            "cache_write": 2.5,
            "output": 12.0,
            "long_context_threshold": 272_000,
            "long_input": 4.0,
            "long_cached_input": 0.4,
            "long_cache_write": 5.0,
            "long_output": 18.0,
        },
    ],
    "gpt-6-sol": [
        {
            "effective": "2026-09-22",
            "input": 2.0,
            "cached_input": 0.2,
            "cache_write": 2.5,
            "output": 10.0,
            "long_context_threshold": 272_000,
            "long_input": 4.0,
            "long_cached_input": 0.4,
            "long_cache_write": 5.0,
            "long_output": 15.0,
        }
    ],
    "gpt-6-luna": [
        {
            "effective": "2026-09-22",
            "input": 0.1,
            "cached_input": 0.01,
            "cache_write": 0.125,
            "output": 0.5,
            "long_context_threshold": 272_000,
            "long_input": 0.2,
            "long_cached_input": 0.02,
            "long_cache_write": 0.25,
            "long_output": 0.75,
        }
    ],
    "claude-opus-4-8": [
        {
            "effective": "2026-01-01",
            "input": 5.0,
            "cache_write_5m": 6.25,
            "cache_write_1h": 10.0,
            "cached_input": 0.5,
            "output": 25.0,
        }
    ],
    "claude-opus-5": [
        {
            "effective": "2026-07-24",
            "input": 5.0,
            "cache_write_5m": 6.25,
            "cache_write_1h": 10.0,
            "cached_input": 0.5,
            "output": 25.0,
        }
    ],
    "claude-opus-5-5": [
        {
            "effective": "2026-09-22",
            "input": 4.0,
            "cache_write_5m": 5.0,
            "cache_write_1h": 8.0,
            "cached_input": 0.2,
            "output": 20.0,
        }
    ],
}
AI_PRICE_SOURCES = {
    "openai": "https://developers.openai.com/api/docs/changelog",
    "anthropic": "https://www.anthropic.com/claude/opus",
    "cache_policy": "https://developers.openai.com/api/docs/pricing",
}


@dataclasses.dataclass
class ToolCall:
    command: str
    name: str
    started: dt.datetime
    seconds: float
    category: str | None
    mutates: bool


@dataclasses.dataclass
class Session:
    provider: str
    session_id: str
    model: str | None
    phase: str
    transcript: Path | None
    started: dt.datetime | None
    ended: dt.datetime | None
    tools: list[ToolCall]
    tokens: dict[str, int]
    roadmap_area: str | None
    roadmap_bytes: int | None
    orientation_to_claim_seconds: float | None
    orientation_to_edit_seconds: float | None
    pr_number: int | None
    api_equivalent_usd: float | None


@dataclasses.dataclass
class ParsedTranscript:
    tools: list[ToolCall]
    tokens: dict[str, int]
    started: dt.datetime | None
    ended: dt.datetime | None
    model: str | None
    opened_pr: int | None


def _time(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _percentile(values: Iterable[float], fraction: float) -> float | None:
    xs = sorted(values)
    if not xs:
        return None
    return xs[min(len(xs) - 1, max(0, math.ceil(fraction * len(xs)) - 1))]


def distribution(values: Iterable[float]) -> dict[str, float | int | None]:
    xs = list(values)
    return {
        "n": len(xs),
        "mean": statistics.fmean(xs) if xs else None,
        "p25": _percentile(xs, 0.25),
        "median": statistics.median(xs) if xs else None,
        "p75": _percentile(xs, 0.75),
        "p90": _percentile(xs, 0.9),
    }


def _phase(text: str) -> str:
    if "You are authoring a new pull request" in text:
        return "preparation"
    if "You are addressing AI code review" in text:
        return "revision"
    if "You are fixing FAILING CI" in text:
        return "fix-ci"
    if "You are reconciling the branch with current main" in text:
        return "rebase"
    if "You are adapting TauCetiProject/TauCeti" in text and "Mathlib bump" in text:
        return "bump"
    return "other"


def _category(command: str) -> str | None:
    # Match programs at shell-command boundaries. Looking for arbitrary substrings would count
    # inspection commands such as `rg "lake build"` as builds, which is both common and wrong.
    unquoted = []
    quote = None
    escaped = False
    for char in command:
        if escaped:
            unquoted.append(" ")
            escaped = False
        elif char == "\\":
            unquoted.append(" ")
            escaped = True
        elif quote:
            unquoted.append(" ")
            if char == quote:
                quote = None
        elif char in "'\"":
            unquoted.append(" ")
            quote = char
        else:
            unquoted.append(char)
    command = "".join(unquoted)
    wrappers = (
        r"(?:(?:command|time)\s+|"
        r"(?:env|nice|stdbuf|timeout)(?:\s+(?:-[^\s]+|[A-Za-z_][A-Za-z0-9_]*=[^\s]+|\d+(?:\.\d+)?[smhd]?))*\s+)*"
    )
    start = (
        r"(?:^|[\n;&|])\s*(?:(?:if|then|elif|do)\s+)?!?\s*"
        r"(?:[A-Za-z_][A-Za-z0-9_]*=[^\s;&|]+\s+)*" + wrappers
    )
    kinds = []
    if re.search(start + r"lake\s+exe\s+cache\s+get!?\b|" + start + r"lake\s+cache\s+get\b", command):
        kinds.append("cache-get")
    if re.search(start + r"lake\s+build\b", command):
        kinds.append("lake-build")
    if re.search(start + r"lake\s+exe\s+axioms\b", command):
        kinds.append("axioms")
    if re.search(start + r"(?:lake\s+env\s+)?lean\s+[^;&|\n]*\.lean\b", command):
        kinds.append("lean-check")
    if not kinds:
        return None
    return kinds[0] if len(kinds) == 1 else "mixed-lean"


def _mutates(name: str, command: str) -> bool:
    if name.lower() in {"edit", "write", "apply_patch", "notebookedit", "multiedit"}:
        return True
    if re.search(
        r"\bapply_patch\b|\bsed\s+-i\b|\bperl\s+-[^\s]*i|\b(?:mv|cp|touch)\s+|"
        r"\.write_(?:text|bytes)\s*\(",
        command,
    ):
        return True

    # Shell diagnostics commonly contain `2>/dev/null` or `>&2`; neither edits a source file.
    # Only classify output from a writer primitive when it is redirected to a real path. Strip
    # quoted text first so an `rg 'echo x > file'` search is not itself an edit.
    unquoted = []
    quote = None
    escaped = False
    for char in command:
        if escaped:
            unquoted.append(" ")
            escaped = False
        elif char == "\\":
            unquoted.append(" ")
            escaped = True
        elif quote:
            unquoted.append(" ")
            if char == quote:
                quote = None
        elif char in "'\"":
            unquoted.append(" ")
            quote = char
        else:
            unquoted.append(char)
    shell = "".join(unquoted)
    if not re.search(r"(?:^|[;&|]\s*)(?:cat|printf|echo)\b", shell):
        return False
    targets = re.findall(r"(?<![0-9&])(?:>>?|>\|)\s*([^\s;&|]+)", shell)
    if any(target not in {"/dev/null", "&1", "&2"} for target in targets):
        return True
    return bool(re.search(r"\btee(?:\s+-[a-zA-Z]+)*\s+(?!/dev/null\b)[^\s;&|]+", shell))


def _model_family(provider: str, model: str | None) -> str | None:
    value = (model or "").lower()
    if provider == "codex":
        if "terra" in value:
            return "terra"
        if "luna" in value:
            return "luna"
        if not value or "sol" in value or value == "gpt-5.5":
            return "sol"
    if provider == "claude" and (not value or "opus" in value):
        return "opus"
    return None


def _price_window(model: str | None, when: dt.datetime | str | None) -> dict | None:
    windows = AI_PRICES.get(model or "")
    if not windows:
        return None
    if isinstance(when, dt.datetime):
        date = when.date().isoformat()
    else:
        date = str(when or "9999-12-31")[:10]
    eligible = [window for window in windows if window["effective"] <= date]
    return eligible[-1] if eligible else windows[0]


def _api_cost(
    provider: str, model: str | None, tokens: dict[str, int], when: dt.datetime | str | None = None
) -> float | None:
    """Price one measured session, preserving provider-specific prompt-cache semantics."""
    family = _model_family(provider, model)
    price = _price_window(model, when)
    if family is None or price is None or not tokens:
        return None
    if provider == "codex":
        have_tiers = "standard_input_tokens" in tokens or "long_input_tokens" in tokens
        if have_tiers:
            standard_input = tokens.get("standard_input_tokens", 0)
            standard_cached = tokens.get("standard_cached_input_tokens", 0)
            standard_write = tokens.get("standard_cache_write_input_tokens", 0)
            standard_output = tokens.get("standard_output_tokens", 0)
            long_input = tokens.get("long_input_tokens", 0)
            long_cached = tokens.get("long_cached_input_tokens", 0)
            long_write = tokens.get("long_cache_write_input_tokens", 0)
            long_output = tokens.get("long_output_tokens", 0)
        elif tokens.get("input_tokens", 0) > price["long_context_threshold"]:
            standard_input = standard_cached = standard_write = standard_output = 0
            long_input = tokens.get("input_tokens", 0)
            long_cached = tokens.get("cached_input_tokens", 0)
            long_write = tokens.get("cache_write_input_tokens", 0)
            long_output = tokens.get("output_tokens", 0)
        else:
            standard_input = tokens.get("input_tokens", 0)
            standard_cached = tokens.get("cached_input_tokens", 0)
            standard_write = tokens.get("cache_write_input_tokens", 0)
            standard_output = tokens.get("output_tokens", 0)
            long_input = long_cached = long_write = long_output = 0
        return (
            (standard_input - standard_cached - standard_write) * price["input"]
            + standard_cached * price["cached_input"]
            + standard_write * price["cache_write"]
            + standard_output * price["output"]
            + (long_input - long_cached - long_write) * price["long_input"]
            + long_cached * price["long_cached_input"]
            + long_write * price["long_cache_write"]
            + long_output * price["long_output"]
        ) / 1e6

    created = tokens.get("cache_creation_input_tokens", 0)
    created_1h = tokens.get("cache_creation_1h_input_tokens", 0)
    created_5m = tokens.get("cache_creation_5m_input_tokens", 0)
    # Older Claude records do not split cache writes by TTL. Price the unsplit remainder at the
    # cheaper 5-minute rate instead of silently treating it as ordinary input.
    unsplit_created = max(0, created - created_1h - created_5m)
    return (
        tokens.get("input_tokens", 0) * price["input"]
        + (created_5m + unsplit_created) * price["cache_write_5m"]
        + created_1h * price["cache_write_1h"]
        + tokens.get("cache_read_input_tokens", 0) * price["cached_input"]
        + tokens.get("output_tokens", 0) * price["output"]
    ) / 1e6


def _command_from_codex(name: str, raw: str) -> str:
    try:
        value = json.loads(raw)
        if isinstance(value, dict):
            return str(value.get("cmd") or value.get("input") or raw)
    except (json.JSONDecodeError, TypeError):
        pass
    # Modern Codex wraps exec_command in a short JavaScript program.
    match = re.search(r"tools\.exec_command\(\s*(\{.*?\})\s*\)", raw, re.S)
    if match:
        try:
            value = json.loads(match.group(1))
            return str(value.get("cmd") or raw)
        except json.JSONDecodeError:
            pass
    return raw


def _output_text(value) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(str(x.get("text", "")) for x in value if isinstance(x, dict))
    return json.dumps(value, default=str)


def _parse_codex(path: Path) -> ParsedTranscript:
    pending: dict[str, tuple[str, str, dt.datetime]] = {}
    process_owner: dict[str, ToolCall] = {}
    tools: list[ToolCall] = []
    tokens: dict[str, int] = {}
    tier_tokens: defaultdict[str, int] = defaultdict(int)
    seen_token_totals: set[tuple[tuple[str, int], ...]] = set()
    modern_commands: list[ToolCall] = []
    first = last = None
    model = None
    opened_pr = None
    with path.open(errors="replace") as handle:
        for line in handle:
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            when = _time(row.get("timestamp"))
            if when:
                first = first or when
                last = when
            payload = row.get("payload") or {}
            typ = payload.get("type")
            if row.get("type") == "turn_context" and isinstance(payload.get("model"), str):
                model = payload["model"]
            if typ == "message" and payload.get("role") == "assistant":
                content = payload.get("content") or []
                text = (
                    content
                    if isinstance(content, str)
                    else "\n".join(str(item.get("text", "")) for item in content if isinstance(item, dict))
                )
                matches = OPENED_PR_RE.findall(text)
                if matches:
                    opened_pr = int(matches[-1])
            elif row.get("type") == "event_msg" and typ == "agent_message":
                matches = OPENED_PR_RE.findall(str(payload.get("message") or ""))
                if matches:
                    opened_pr = int(matches[-1])
            if row.get("type") == "event_msg" and typ == "item_completed":
                item = payload.get("item") or {}
                if item.get("type") == "CommandExecution":
                    command_value = item.get("command") or []
                    if isinstance(command_value, list):
                        command = str(command_value[-1]) if command_value else ""
                    else:
                        command = str(command_value)
                    started_ms = item.get("started_at_ms")
                    completed_ms = item.get("completed_at_ms")
                    duration = item.get("duration") or {}
                    if isinstance(started_ms, (int, float)):
                        command_started = dt.datetime.fromtimestamp(started_ms / 1000, dt.UTC)
                    else:
                        command_started = when
                    if isinstance(completed_ms, (int, float)) and isinstance(started_ms, (int, float)):
                        seconds = max(0.0, (completed_ms - started_ms) / 1000)
                    else:
                        seconds = float(duration.get("secs", 0)) + float(duration.get("nanos", 0)) / 1e9
                    if command_started:
                        modern_commands.append(
                            ToolCall(
                                command,
                                "exec_command",
                                command_started,
                                seconds,
                                _category(command),
                                _mutates("exec_command", command),
                            )
                        )
            if row.get("type") == "event_msg" and typ == "token_count":
                info = payload.get("info") or {}
                usage = info.get("total_token_usage") or {}
                tokens = {k: int(v) for k, v in usage.items() if isinstance(v, (int, float))}
                signature = tuple(sorted(tokens.items()))
                last_usage = info.get("last_token_usage") or {}
                if signature not in seen_token_totals and last_usage:
                    seen_token_totals.add(signature)
                    prefix = "long_" if last_usage.get("input_tokens", 0) > LONG_CONTEXT_THRESHOLD else "standard_"
                    for key in (
                        "input_tokens",
                        "cached_input_tokens",
                        "cache_write_input_tokens",
                        "output_tokens",
                    ):
                        value = last_usage.get(key)
                        if isinstance(value, (int, float)):
                            tier_tokens[prefix + key] += int(value)
            if typ in {"function_call", "custom_tool_call"} and when:
                call_id = payload.get("call_id")
                name = str(payload.get("name") or "")
                raw = str(payload.get("arguments") or payload.get("input") or "")
                if call_id:
                    pending[call_id] = (name, raw, when)
            elif typ in {"function_call_output", "custom_tool_call_output"} and when:
                call_id = payload.get("call_id")
                if not call_id or call_id not in pending:
                    continue
                name, raw, started = pending.pop(call_id)
                if name == "exec":
                    nested = re.search(r"\btools\.(exec_command|write_stdin|wait)\s*\(", raw)
                    if nested:
                        name = f"wrapped-{nested.group(1)}"
                out = _output_text(payload.get("output"))
                wall = WALL_RE.search(out)
                seconds = float(wall.group(1)) if wall else max(0.0, (when - started).total_seconds())
                command = _command_from_codex(name, raw)
                # Polls belong to the original long-running command, not to a new tool category.
                poll_id = None
                if name in {"write_stdin", "wait", "wrapped-write_stdin", "wrapped-wait"}:
                    try:
                        args = json.loads(raw)
                        poll_id = str(args.get("session_id") or args.get("cell_id") or "")
                    except json.JSONDecodeError:
                        match = re.search(r"\b(?:session_id|cell_id)\s*:\s*['\"]?([A-Za-z0-9_-]+)", raw)
                        poll_id = match.group(1) if match else None
                if poll_id and poll_id in process_owner:
                    process_owner[poll_id].seconds += seconds
                    continue
                tool = ToolCall(command, name, started, seconds, _category(command), _mutates(name, command))
                tools.append(tool)
                process = PROCESS_RE.search(out)
                if process:
                    process_owner[process.group(1)] = tool
    if modern_commands:
        # Current Codex emits an exact CommandExecution item for every unified `exec` wrapper.
        # Drop only shell wrappers and their polls. Other calls such as tools.apply_patch are also
        # wrapped in outer `exec` records but have no CommandExecution counterpart and must survive.
        duplicated = {"wrapped-exec_command", "wrapped-write_stdin", "wrapped-wait"}
        tools = [tool for tool in tools if tool.name not in duplicated]
    tools.extend(modern_commands)
    tools.sort(key=lambda tool: tool.started)
    if sum(tier_tokens[key] for key in ("standard_input_tokens", "long_input_tokens")) == tokens.get("input_tokens", 0):
        tokens.update(tier_tokens)
    return ParsedTranscript(tools, tokens, first, last, model, opened_pr)


def _parse_claude(path: Path) -> ParsedTranscript:
    pending: dict[str, tuple[str, str, dt.datetime]] = {}
    tools: list[ToolCall] = []
    totals: defaultdict[str, int] = defaultdict(int)
    seen_usage: set[str] = set()
    first = last = None
    model = None
    opened_pr = None
    with path.open(errors="replace") as handle:
        for line in handle:
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            when = _time(row.get("timestamp"))
            if when:
                first = first or when
                last = when
            message = row.get("message") or {}
            usage = message.get("usage") or {}
            if row.get("type") == "assistant":
                candidate_model = message.get("model")
                if isinstance(candidate_model, str) and candidate_model != "<synthetic>":
                    model = candidate_model
                # Claude writes one assistant row per content block and repeats the complete
                # message usage on each row. Count a message id once; requestId is the fallback
                # for older rows. The numeric counters are identical across repeated rows.
                usage_id = str(message.get("id") or row.get("requestId") or row.get("uuid"))
                if usage and usage_id not in seen_usage:
                    seen_usage.add(usage_id)
                    for key, value in usage.items():
                        if isinstance(value, (int, float)):
                            totals[key] += int(value)
                    cache_creation = usage.get("cache_creation") or {}
                    if isinstance(cache_creation, dict):
                        one_hour = cache_creation.get("ephemeral_1h_input_tokens")
                        five_minute = cache_creation.get("ephemeral_5m_input_tokens")
                        if isinstance(one_hour, (int, float)):
                            totals["cache_creation_1h_input_tokens"] += int(one_hour)
                        if isinstance(five_minute, (int, float)):
                            totals["cache_creation_5m_input_tokens"] += int(five_minute)
                for item in message.get("content") or []:
                    if not isinstance(item, dict):
                        continue
                    if item.get("type") == "text":
                        matches = OPENED_PR_RE.findall(str(item.get("text") or ""))
                        if matches:
                            opened_pr = int(matches[-1])
                    if item.get("type") != "tool_use" or not when:
                        continue
                    inp = item.get("input") or {}
                    command = str(inp.get("command") or inp.get("patch") or inp)
                    pending[str(item.get("id"))] = (str(item.get("name") or ""), command, when)
            elif row.get("type") == "user" and when:
                for item in message.get("content") or []:
                    if not isinstance(item, dict) or item.get("type") != "tool_result":
                        continue
                    tool_id = str(item.get("tool_use_id"))
                    if tool_id not in pending:
                        continue
                    name, command, started = pending.pop(tool_id)
                    seconds = max(0.0, (when - started).total_seconds())
                    tools.append(ToolCall(command, name, started, seconds, _category(command), _mutates(name, command)))
    # Claude stores delegated agents below <session-id>/subagents rather than in the main JSONL.
    # Their tokens are part of the cost of the parent Tau Ceti round, and their edits/builds are
    # part of its observed work. Recursing also handles a delegated agent spawning another one.
    subagents = path.with_suffix("") / "subagents"
    if subagents.is_dir():
        for child in subagents.glob("*.jsonl"):
            parsed = _parse_claude(child)
            tools.extend(parsed.tools)
            for key, value in parsed.tokens.items():
                totals[key] += value
            if parsed.started and (first is None or parsed.started < first):
                first = parsed.started
            if parsed.ended and (last is None or parsed.ended > last):
                last = parsed.ended
    tools.sort(key=lambda tool: tool.started)
    return ParsedTranscript(tools, dict(totals), first, last, model, opened_pr)


def _transcript_index(state_dir: Path) -> dict[str, Path]:
    found = {}
    if not state_dir.is_dir():
        return found
    patterns = ("*/home/.codex/sessions/**/*.jsonl", "*/home/.claude/projects/**/*.jsonl")
    for pattern in patterns:
        for path in state_dir.glob(pattern):
            stem = path.stem
            session_id = stem.rsplit("-", 5)[-5:] if stem.startswith("rollout-") else None
            if session_id:
                candidate = "-".join(session_id)
                if re.fullmatch(r"[0-9a-f-]{36}", candidate):
                    found[candidate] = path
            elif re.fullmatch(r"[0-9a-f-]{36}", stem):
                found[stem] = path
    return found


def _initial_prompt(path: Path, provider: str) -> str:
    """Return the first real user prompt, excluding Claude's queue/attachment records."""
    collected = []
    try:
        with path.open(errors="replace") as handle:
            for line_no, line in enumerate(handle):
                if line_no > 200:
                    break
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                message = (row.get("message") if provider == "claude" else (row.get("payload") or {})) or {}
                if provider == "codex":
                    if message.get("type") != "message" or message.get("role") != "user":
                        continue
                elif row.get("type") != "user" or message.get("role") != "user":
                    continue
                content = message.get("content")
                if isinstance(content, str):
                    collected.append(content)
                if isinstance(content, list):
                    texts = [
                        str(item.get("text", ""))
                        for item in content
                        if isinstance(item, dict) and item.get("type") in {"input_text", "text"}
                    ]
                    if texts:
                        collected.extend(texts)
                joined = "\n".join(collected)
                if _phase(joined) != "other":
                    return joined
    except OSError:
        pass
    return "\n".join(collected)


def _roadmap_area(text: str) -> str | None:
    claim = CLAIM_RE.search(text)
    if claim:
        return claim.group(1)
    for pattern in (
        r"Work ONLY within the `([^`]+)` roadmap",
        r"designated roadmap.*?`([^`]+)`",
        r"TauCetiRoadmap/([^/`\s]+)/README\.md",
    ):
        match = re.search(pattern, text, re.S | re.I)
        if match:
            area = match.group(1)
            if "<" not in area and ">" not in area:
                return area
    return None


def _roadmap_size(state_dir: Path, worker: str, area: str | None) -> int | None:
    if not area or area.lower() in {"any", "auto", "none"}:
        return None
    choices = [
        state_dir / worker / "refs" / "roadmap" / "TauCetiRoadmap" / area / "README.md",
        state_dir / "default" / "refs" / "roadmap" / "TauCetiRoadmap" / area / "README.md",
    ]
    for path in choices:
        try:
            return path.stat().st_size
        except OSError:
            pass
    return None


def analyze(logs_dir: Path, state_dir: Path, limit: int = 250) -> dict:
    index = _transcript_index(state_dir)
    candidates = []
    for session_id, transcript in index.items():
        try:
            candidates.append((transcript.stat().st_mtime, session_id, transcript))
        except OSError:
            continue
    candidates.sort(reverse=True, key=lambda row: row[0])
    if limit:
        candidates = candidates[:limit]

    sessions: list[Session] = []
    unreadable = 0
    for _, session_id, transcript in candidates:
        provider = "codex" if ".codex" in transcript.parts else "claude"
        try:
            parsed = _parse_codex(transcript) if provider == "codex" else _parse_claude(transcript)
        except OSError:
            unreadable += 1
            continue
        prompt = _initial_prompt(transcript, provider)
        phase = _phase(prompt)
        area = None
        if phase == "preparation":
            # The command that actually acquired the claim is authoritative. Prompts contain
            # examples such as author/<target-roadmap>/..., which must never become an area.
            for tool in parsed.tools:
                claim = CLAIM_RE.search(tool.command)
                if claim:
                    area = claim.group(1)
                    break
            area = area or _roadmap_area(prompt)
        try:
            worker = transcript.relative_to(state_dir).parts[0]
        except (ValueError, IndexError):
            worker = "default"
        claim_times = [t.started for t in parsed.tools if CLAIM_RE.search(t.command)]
        edit_times = [t.started for t in parsed.tools if t.mutates]
        prompt_pr = PR_RE.search(prompt)
        pr_number = parsed.opened_pr if phase == "preparation" else int(prompt_pr.group(1)) if prompt_pr else None
        api_cost = _api_cost(provider, parsed.model, parsed.tokens, parsed.started)
        sessions.append(
            Session(
                provider,
                session_id,
                parsed.model,
                phase,
                transcript,
                parsed.started,
                parsed.ended,
                parsed.tools,
                parsed.tokens,
                area,
                _roadmap_size(state_dir, worker, area),
                (claim_times[0] - parsed.started).total_seconds() if parsed.started and claim_times else None,
                (edit_times[0] - parsed.started).total_seconds() if parsed.started and edit_times else None,
                pr_number,
                api_cost,
            )
        )

    groups = {}
    for (provider, phase), rows in _group(sessions, lambda s: (s.provider, s.phase)).items():
        lean_seconds = [sum(t.seconds for t in s.tools if t.category) for s in rows]
        categories = {}
        for category in ("cache-get", "lake-build", "axioms", "lean-check", "mixed-lean"):
            calls = [t.seconds for s in rows for t in s.tools if t.category == category]
            per_session = [sum(t.seconds for t in s.tools if t.category == category) for s in rows]
            if calls:
                categories[category] = {
                    "calls": distribution(calls),
                    "seconds_per_session": distribution(per_session),
                    "total_hours": sum(calls) / 3600,
                }
        groups[f"{provider}/{phase}"] = {
            "sessions": len(rows),
            "session_wall_minutes": distribution(
                (s.ended - s.started).total_seconds() / 60 for s in rows if s.started and s.ended
            ),
            "lean_tool_seconds_per_session": distribution(lean_seconds),
            "api_equivalent_usd_per_session": distribution(
                s.api_equivalent_usd for s in rows if s.api_equivalent_usd is not None
            ),
            "tools": categories,
        }

    ai_groups = {}
    for (family, phase), rows in _group(
        [s for s in sessions if _model_family(s.provider, s.model)],
        lambda s: (_model_family(s.provider, s.model), s.phase),
    ).items():
        priced = [s for s in rows if s.api_equivalent_usd is not None]
        token_totals: defaultdict[str, int] = defaultdict(int)
        for session in priced:
            for key, value in session.tokens.items():
                token_totals[key] += value
        ai_groups[f"{family}/{phase}"] = {
            "sessions": len(rows),
            "priced_sessions": len(priced),
            "prs_observed": len({s.pr_number for s in rows if s.pr_number is not None}),
            "priced_prs_observed": len({s.pr_number for s in priced if s.pr_number is not None}),
            "api_equivalent_usd_per_session": distribution(s.api_equivalent_usd for s in priced),
            "token_totals": dict(token_totals),
        }

    prep = [s for s in sessions if s.phase == "preparation"]
    orientation = {
        "sessions": len(prep),
        "sessions_without_claim_timing": sum(s.orientation_to_claim_seconds is None for s in prep),
        "sessions_without_first_edit_timing": sum(s.orientation_to_edit_seconds is None for s in prep),
        "sessions_without_roadmap_size": sum(s.roadmap_bytes is None for s in prep),
        "to_claim_seconds": distribution(
            s.orientation_to_claim_seconds for s in prep if s.orientation_to_claim_seconds is not None
        ),
        "to_first_edit_seconds": distribution(
            s.orientation_to_edit_seconds for s in prep if s.orientation_to_edit_seconds is not None
        ),
        "roadmap_bytes": distribution(float(s.roadmap_bytes) for s in prep if s.roadmap_bytes is not None),
    }
    pairs = [
        (float(s.roadmap_bytes), s.orientation_to_claim_seconds)
        for s in prep
        if s.roadmap_bytes is not None and s.orientation_to_claim_seconds is not None
    ]
    orientation["roadmap_bytes_vs_claim_spearman"] = _spearman(pairs)
    prior_pairs = []
    prior_bins: defaultdict[str, list[float]] = defaultdict(list)
    seen_areas: defaultdict[str, int] = defaultdict(int)
    for session in sorted(prep, key=lambda s: s.started or dt.datetime.min.replace(tzinfo=dt.UTC)):
        if not session.roadmap_area:
            continue
        prior = seen_areas[session.roadmap_area]
        seen_areas[session.roadmap_area] += 1
        if session.orientation_to_claim_seconds is None:
            continue
        prior_pairs.append((float(prior), session.orientation_to_claim_seconds))
        label = "0" if prior == 0 else "1-4" if prior < 5 else "5+"
        prior_bins[label].append(session.orientation_to_claim_seconds)
    orientation["prior_local_sessions_in_area_vs_claim_spearman"] = _spearman(prior_pairs)
    orientation["claim_seconds_by_prior_local_sessions_in_area"] = {
        label: distribution(values) for label, values in sorted(prior_bins.items())
    }

    return {
        "schema": 2,
        "coverage": {
            "transcripts_available": len(index),
            "transcripts_considered": len(candidates),
            "sessions_parsed": len(sessions),
            "unreadable_transcripts": unreadable,
            "agent_logs_available": sum(1 for _ in logs_dir.glob("*/agent-*.log")),
            "limit": limit,
        },
        "groups": groups,
        "ai_sessions": {
            "pricing": {"usd_per_million_tokens": AI_PRICES, "sources": AI_PRICE_SOURCES},
            "groups": ai_groups,
            "preparation_prs_observed": len({s.pr_number for s in prep if s.pr_number is not None}),
            "priced_preparation_prs_observed": len(
                {s.pr_number for s in prep if s.pr_number is not None and s.api_equivalent_usd is not None}
            ),
            "note": "tokens measured from local transcripts; dollars imputed at the displayed cache-aware rates",
        },
        "orientation": orientation,
    }


def _group(values, key):
    out = defaultdict(list)
    for value in values:
        out[key(value)].append(value)
    return out


def _ranks(values: list[float]) -> list[float]:
    order = sorted(range(len(values)), key=values.__getitem__)
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i + 1
        while j < len(order) and values[order[j]] == values[order[i]]:
            j += 1
        rank = (i + j - 1) / 2
        for k in order[i:j]:
            ranks[k] = rank
        i = j
    return ranks


def _spearman(pairs: list[tuple[float, float]]) -> dict[str, float | int | None]:
    if len(pairs) < 3:
        return {"n": len(pairs), "rho": None}
    xs, ys = map(list, zip(*pairs, strict=True))
    rx, ry = _ranks(xs), _ranks(ys)
    mx, my = statistics.fmean(rx), statistics.fmean(ry)
    numerator = sum((x - mx) * (y - my) for x, y in zip(rx, ry, strict=True))
    denominator = math.sqrt(sum((x - mx) ** 2 for x in rx) * sum((y - my) ** 2 for y in ry))
    return {"n": len(pairs), "rho": numerator / denominator if denominator else None}


def analyze_review_archive(data_dir: Path | None) -> dict:
    """Aggregate the canonical production review records already present on this machine."""
    runs_dir = data_dir / "records" / "runs" if data_dir else None
    if runs_dir is None or not runs_dir.is_dir():
        return {
            "available": False,
            "data_dir": str(data_dir) if data_dir else None,
            "note": "no TauCetiData records/runs archive found; pass --review-data-dir",
        }

    seen: set[str] = set()
    pr_costs: defaultdict[int, float] = defaultdict(float)
    pr_runs: defaultdict[int, int] = defaultdict(int)
    pr_rounds: defaultdict[int, int] = defaultdict(int)
    family_costs: defaultdict[str, list[float]] = defaultdict(list)
    family_tokens: defaultdict[str, defaultdict[str, int]] = defaultdict(lambda: defaultdict(int))
    records = priced = estimated = repriced = skipped_shadow = skipped_duplicate = malformed = 0
    dates = []
    for path in runs_dir.glob("*/*.json"):
        records += 1
        try:
            row = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            malformed += 1
            continue
        if not isinstance(row, dict):
            malformed += 1
            continue
        if (row.get("arm") or "production") != "production":
            skipped_shadow += 1
            continue
        key = str(row.get("dedupe_key") or row.get("run_id") or path)
        if key in seen:
            skipped_duplicate += 1
            continue
        seen.add(key)
        try:
            pr = int(row["pr"])
            round_no = int(row.get("round") or 0)
            cost = float(row["cost_usd"])
        except (KeyError, TypeError, ValueError):
            malformed += 1
            continue
        if not math.isfinite(cost) or cost < 0:
            malformed += 1
            continue
        model = str(row.get("model") or "")
        provider = str(row.get("provider") or "")
        is_estimated = bool(row.get("cost_estimated"))
        usage = row.get("usage") or {}
        if is_estimated and isinstance(usage, dict):
            normalized = {key: int(value) for key, value in usage.items() if isinstance(value, (int, float))}
            cache_creation = usage.get("cache_creation") or {}
            if isinstance(cache_creation, dict):
                normalized["cache_creation_1h_input_tokens"] = int(cache_creation.get("ephemeral_1h_input_tokens") or 0)
                normalized["cache_creation_5m_input_tokens"] = int(cache_creation.get("ephemeral_5m_input_tokens") or 0)
            measured_cost = _api_cost(
                "codex" if provider == "codex" else "claude",
                model,
                normalized,
                str(row.get("started_at") or ""),
            )
            if measured_cost is not None:
                cost = measured_cost
                repriced += 1
        priced += 1
        estimated += is_estimated
        pr_costs[pr] += cost
        pr_runs[pr] += 1
        pr_rounds[pr] = max(pr_rounds[pr], round_no)
        family = _model_family("codex" if provider == "codex" else "claude", model)
        if family:
            family_costs[family].append(cost)
            for name, value in (row.get("usage") or {}).items():
                if isinstance(value, (int, float)):
                    family_tokens[family][name] += int(value)
        started = str(row.get("started_at") or "")[:10]
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", started):
            dates.append(started)

    total_runs_per_pr = distribution(float(value) for value in pr_runs.values())
    review_usd_per_pr = distribution(pr_costs.values())
    extra_rounds_per_pr = distribution(float(max(0, value - 1)) for value in pr_rounds.values())
    families = {}
    for family, costs in sorted(family_costs.items()):
        families[family] = {
            "rubric_runs": len(costs),
            "api_equivalent_usd_per_rubric_run": distribution(costs),
            "token_totals": dict(family_tokens[family]),
        }
    return {
        "available": True,
        "data_dir": str(data_dir),
        "records_seen": records,
        "production_runs_priced": priced,
        "estimated_cost_runs": estimated,
        "estimated_cost_runs_repriced": repriced,
        "provider_reported_cost_runs": priced - estimated,
        "shadow_runs_excluded": skipped_shadow,
        "duplicates_excluded": skipped_duplicate,
        "malformed_or_unpriced": malformed,
        "prs": len(pr_costs),
        "date_range": [min(dates), max(dates)] if dates else None,
        "rubric_runs_per_pr": total_runs_per_pr,
        "review_usd_per_pr": review_usd_per_pr,
        "extra_review_rounds_per_pr": extra_rounds_per_pr,
        "families": families,
        "note": (
            "tokens and production-run membership are measured in TauCetiData; Claude costs are provider-reported "
            "where available and estimated costs are recomputed from tokens at the displayed dated prices"
        ),
    }


def measured_ai_model(session_report: dict, review_report: dict, changed_loc_per_pr: float) -> dict:
    """Combine measured phase samples into observed, Sol-only, and Opus-only per-PR scenarios."""

    def group_mean(family: str | None, phase: str) -> float | None:
        groups = session_report["ai_sessions"]["groups"]
        selected = [
            group
            for name, group in groups.items()
            if name.endswith("/" + phase) and (family is None or name.startswith(family + "/"))
        ]
        distributions = [item["api_equivalent_usd_per_session"] for item in selected]
        n = sum(int(item["n"]) for item in distributions)
        if not n:
            return None
        total = sum(float(item["mean"]) * int(item["n"]) for item in distributions)
        if phase == "preparation":
            if family is None:
                denominator = session_report["ai_sessions"]["priced_preparation_prs_observed"]
            else:
                denominator = sum(int(item["priced_prs_observed"]) for item in selected)
            if denominator:
                return total / denominator
        return total / n

    runs_per_pr = (review_report.get("rubric_runs_per_pr") or {}).get("mean")
    reviewed_prs = review_report.get("prs") or 0
    complete_transcript_history = not session_report.get("coverage", {}).get("limit", 0)
    revision_sessions = sum(
        group["priced_sessions"]
        for name, group in session_report["ai_sessions"]["groups"].items()
        if name.endswith("/revision")
    )
    revision_sessions_per_pr = (
        revision_sessions / reviewed_prs if reviewed_prs and complete_transcript_history else None
    )
    scenarios = {}
    for family in ("observed", "sol", "opus"):
        selected = None if family == "observed" else family
        preparation = group_mean(selected, "preparation")
        revision_session = group_mean(selected, "revision")
        if family == "observed":
            review = (review_report.get("review_usd_per_pr") or {}).get("mean")
        else:
            review_run = (
                review_report.get("families", {})
                .get(family, {})
                .get("api_equivalent_usd_per_rubric_run", {})
                .get("mean")
            )
            review = review_run * runs_per_pr if review_run is not None and runs_per_pr is not None else None
        revision = (
            revision_session * revision_sessions_per_pr
            if revision_session is not None and revision_sessions_per_pr is not None
            else None
        )
        components = {"preparation": preparation, "reviews": review, "revisions": revision}
        if all(value is not None for value in components.values()):
            total = sum(components.values())
            scenarios[family] = {
                "usd_per_pr": total,
                "usd_per_changed_loc": total / changed_loc_per_pr,
                "components_usd_per_pr": components,
            }
        else:
            scenarios[family] = {
                "usd_per_pr": None,
                "usd_per_changed_loc": None,
                "components_usd_per_pr": components,
                "note": "insufficient local samples for every phase",
            }
    return {
        "changed_loc_per_pr": changed_loc_per_pr,
        "revision_sessions_per_reviewed_pr": revision_sessions_per_pr,
        "scenarios": scenarios,
        "method": (
            "preparation-attempt cost amortized over observed opened PRs + mean review lifecycle cost + "
            "mean revision-session cost times observed revision sessions per reviewed PR; divided by measured "
            "mean changed LOC per PR"
        ),
        "caveat": (
            "API-equivalent pricing of subscription usage; phase samples and review archive are observational "
            "and do not prove that model choice causes the observed cost difference"
        ),
    }


def infrastructure_model(
    *,
    pr_builds: float,
    pr_runner_minutes: float,
    pr_runner_vcpus: float,
    main_runner_minutes: float,
    main_runner_vcpus: float,
    pr_runner_usd_minute: float,
    main_runner_usd_minute: float,
    cache_objects: int,
    cache_gib: float,
    retained_cache_gib: float,
    merged_prs_month: float | None = None,
    pr_component_shares: tuple[float, float, float] = (1.86 / 8.77, 5.44 / 8.77, 1.47 / 8.77),
    main_component_shares: tuple[float, float, float] = (3.42 / 14.07, 1.15 / 14.07, 9.50 / 14.07),
) -> dict:
    def normalized(values: tuple[float, float, float], label: str) -> tuple[float, float, float]:
        if len(values) != 3 or any(not math.isfinite(value) or value < 0 for value in values):
            raise ValueError(f"{label} must contain three non-negative finite values")
        total = sum(values)
        if total <= 0:
            raise ValueError(f"{label} may not be all zero")
        return tuple(value / total for value in values)

    pr_component_shares = normalized(pr_component_shares, "pr_component_shares")
    main_component_shares = normalized(main_component_shares, "main_component_shares")
    pr_minutes = pr_builds * pr_runner_minutes
    vcpu_hours = (pr_minutes * pr_runner_vcpus + main_runner_minutes * main_runner_vcpus) / 60
    fetch_cost = (cache_objects + 1) / 1_000_000 * 0.36
    fetches_per_pr = pr_builds + 1  # branch/merge-queue builds plus post-merge main CI
    cache_fetch = {
        "objects": cache_objects,
        "gib": cache_gib,
        "gross_class_b_usd": fetch_cost,
        "fetches_per_merged_pr": fetches_per_pr,
        "gross_class_b_usd_per_merged_pr": fetch_cost * fetches_per_pr,
        "gib_per_merged_pr": cache_gib * fetches_per_pr,
        "egress_usd": 0.0,
        "note": "gross Standard-tier requests before Cloudflare's monthly free allowance",
    }
    if merged_prs_month is not None:
        monthly_requests = (cache_objects + 1) * fetches_per_pr * merged_prs_month
        cache_fetch["monthly_at_merged_prs"] = merged_prs_month
        cache_fetch["monthly_class_b_requests"] = monthly_requests
        cache_fetch["monthly_gib"] = cache_gib * fetches_per_pr * merged_prs_month
        cache_fetch["monthly_class_b_usd_after_free_tier"] = max(0, monthly_requests - 10_000_000) / 1_000_000 * 0.36
    component_names = ("fixed", "touched", "repository")
    decomposition = {}
    for name, pr_share, main_share in zip(component_names, pr_component_shares, main_component_shares, strict=True):
        branch_minutes = pr_minutes * pr_share
        postmerge_minutes = main_runner_minutes * main_share
        decomposition[name] = {
            "runner_minutes_per_merged_pr": branch_minutes + postmerge_minutes,
            "equivalent_usd_per_merged_pr": (
                branch_minutes * pr_runner_usd_minute + postmerge_minutes * main_runner_usd_minute
            ),
            "pr_build_share": pr_share,
            "postmerge_share": main_share,
        }
    retained_cache_gb = retained_cache_gib * (2**30 / 1e9)
    return {
        "ci": {
            "branch_and_merge_queue_builds_per_merged_pr": pr_builds,
            "runner_minutes_per_merged_pr": pr_minutes + main_runner_minutes,
            "vcpu_hours_per_merged_pr": vcpu_hours,
            "equivalent_usd_per_merged_pr": (
                pr_minutes * pr_runner_usd_minute + main_runner_minutes * main_runner_usd_minute
            ),
            "decomposition": decomposition,
            "decomposition_note": (
                "proxy classification, not a causal fit: fixed/setup, candidate build work, and "
                "whole-repository checks from 40 PR jobs and 20 post-merge jobs sampled 2026-09-18; "
                "a 97-run cross-check found wall-time Pearson correlations of 0.06 with changed LOC "
                "and 0.04 with changed-file count"
            ),
        },
        "r2_complete_cache_fetch": cache_fetch,
        "r2_complete_cache_publish": {
            "gross_class_a_usd": (cache_objects + 1) / 1_000_000 * 4.50,
            "note": "upper bound if every archive is newly written; content addressing normally deduplicates most objects",
        },
        "r2_storage": {
            "estimated_retained_gib": retained_cache_gib,
            "estimated_retained_gb": retained_cache_gb,
            "gross_usd_month": retained_cache_gb * 0.015,
            "usd_month_after_10_gb_free_tier": max(0.0, retained_cache_gb - 10) * 0.015,
            "note": "estimate from locally retained TauCeti mappings and current archive-size sample",
        },
    }


def loc_cost_model(
    infrastructure: dict,
    *,
    ai_usd_per_changed_loc: float,
    changed_loc_per_pr: float,
    changed_per_net_loc: float,
    reference_repo_loc: float,
    projection_locs: Iterable[float],
) -> dict:
    """Turn the per-PR infrastructure estimate into an explicit LOC scaling scenario.

    Fixed and touched-code CI are held constant per changed LOC. Only checks classified as
    repository-wide, plus a complete R2 cache read, scale linearly with existing repository LOC.
    This is intentionally a scenario rather than a fitted forecast: the available historical CI
    series contains operational changes large enough to swamp the repository-size signal.
    """
    decomposition = infrastructure["ci"]["decomposition"]
    per_changed = {
        name: values["equivalent_usd_per_merged_pr"] / changed_loc_per_pr for name, values in decomposition.items()
    }
    cache_per_changed = (
        infrastructure["r2_complete_cache_fetch"]["gross_class_b_usd_per_merged_pr"] / changed_loc_per_pr
    )
    repository_at_reference = per_changed["repository"] + cache_per_changed
    intercept = ai_usd_per_changed_loc + per_changed["fixed"] + per_changed["touched"]
    slope = repository_at_reference / reference_repo_loc
    integrated_quadratic = slope / 2

    # A deliberately pessimistic comparison with the earlier simple model, in which every dollar
    # of present-day CI and cache cost grows with repository size.
    all_infra_at_reference = sum(per_changed.values()) + cache_per_changed
    upper_slope = all_infra_at_reference / reference_repo_loc

    projections = []
    for loc in projection_locs:
        changed_marginal = intercept + slope * loc
        no_churn_cumulative = intercept * loc + integrated_quadratic * loc * loc
        projections.append(
            {
                "repository_loc": loc,
                "changed_loc": {
                    "marginal_usd": changed_marginal,
                    "no_churn_cumulative_usd": no_churn_cumulative,
                },
                "net_retained_loc": {
                    "marginal_usd": changed_marginal * changed_per_net_loc,
                    "cumulative_usd": no_churn_cumulative * changed_per_net_loc,
                },
            }
        )

    return {
        "reference_repo_loc": reference_repo_loc,
        "changed_loc_per_pr": changed_loc_per_pr,
        "changed_per_net_loc": changed_per_net_loc,
        "current_usd_per_changed_loc": {
            "ai_preparation_review_revision": ai_usd_per_changed_loc,
            "ci_fixed": per_changed["fixed"],
            "ci_touched": per_changed["touched"],
            "ci_repository": per_changed["repository"],
            "r2_repository": cache_per_changed,
            "total": intercept + slope * reference_repo_loc,
        },
        "best_scaling_scenario": {
            "marginal_intercept_usd": intercept,
            "marginal_slope_usd_per_existing_loc": slope,
            "integrated_linear_usd": intercept,
            "integrated_quadratic_usd": integrated_quadratic,
            "retained_integrated_linear_usd": intercept * changed_per_net_loc,
            "retained_integrated_quadratic_usd": integrated_quadratic * changed_per_net_loc,
            "repository_usd_per_changed_loc_at_reference": repository_at_reference,
            "note": (
                "fixed and touched-code CI stay constant per changed LOC; repository-wide CI "
                "and R2 reads grow linearly with existing LOC"
            ),
        },
        "all_infrastructure_scales_scenario": {
            "marginal_intercept_usd": ai_usd_per_changed_loc,
            "marginal_slope_usd_per_existing_loc": upper_slope,
            "integrated_linear_usd": ai_usd_per_changed_loc,
            "integrated_quadratic_usd": upper_slope / 2,
            "note": "pessimistic comparison in which all present-day CI and R2 cost scales with repository LOC",
        },
        "projections": projections,
        "caveat": (
            "scaling scenario, not a forecast; CI architecture, cache behavior, PR size, model "
            "prices, and agent behavior are held fixed"
        ),
    }


def format_report(report: dict) -> str:
    lines = []
    coverage = report["coverage"]
    lines.append(
        f"local sample: {coverage['sessions_parsed']}/{coverage['transcripts_considered']} transcripts parsed"
        + (f" ({coverage['unreadable_transcripts']} unreadable)" if coverage["unreadable_transcripts"] else "")
    )
    lines.append("")
    lines.append("AI API-equivalent cost measured from tokens:")
    for name, group in sorted(report["ai_sessions"]["groups"].items()):
        if not (name.endswith("/preparation") or name.endswith("/revision")):
            continue
        dist = group["api_equivalent_usd_per_session"]
        if dist["n"]:
            coverage_note = f"/{group['sessions']} priced" if group["priced_sessions"] != group["sessions"] else ""
            lines.append(
                f"  {name}: n={dist['n']}{coverage_note}, median=${dist['median']:.2f}, "
                f"p90=${dist['p90']:.2f}, mean=${dist['mean']:.2f} per session"
            )
    review = report.get("ai_reviews") or {}
    if review.get("available"):
        dist = review["review_usd_per_pr"]
        lines.append(
            f"  production reviews: {review['production_runs_priced']:,} rubric runs / {review['prs']:,} PRs; "
            f"median=${dist['median']:.2f}, p90=${dist['p90']:.2f}, mean=${dist['mean']:.2f} per PR"
        )
    ai_model = report.get("ai_cost_model")
    if ai_model:
        lines.append("  measured per-PR scenarios:")
        complete = 0
        for name, scenario in ai_model["scenarios"].items():
            if scenario["usd_per_pr"] is None:
                continue
            complete += 1
            components = scenario["components_usd_per_pr"]
            lines.append(
                f"    {name}: ${scenario['usd_per_pr']:.2f}/PR = preparation ${components['preparation']:.2f} + "
                f"reviews ${components['reviews']:.2f} + revisions ${components['revisions']:.2f}; "
                f"${scenario['usd_per_changed_loc']:.3e}/changed LOC"
            )
        if not complete:
            lines.append("    unavailable: use the full transcript history or supply an explicit AI-cost override")
    lines.append("")
    lines.append("local Lean/cache wall time per session:")
    for name, group in sorted(report["groups"].items()):
        dist = group["lean_tool_seconds_per_session"]
        if dist["n"]:
            lines.append(
                f"  {name}: n={dist['n']}, median={dist['median']:.1f}s, "
                f"p90={dist['p90']:.1f}s, mean={dist['mean']:.1f}s"
            )
    orient = report["orientation"]
    lines.append("")
    lines.append("author orientation (preparation sessions):")
    lines.append(
        f"  coverage: {orient['sessions'] - orient['sessions_without_claim_timing']}/{orient['sessions']} claim, "
        f"{orient['sessions'] - orient['sessions_without_first_edit_timing']}/{orient['sessions']} first-edit, "
        f"{orient['sessions'] - orient['sessions_without_roadmap_size']}/{orient['sessions']} roadmap-size"
    )
    for label, key in (("to target claim", "to_claim_seconds"), ("to first edit", "to_first_edit_seconds")):
        dist = orient[key]
        if dist["n"]:
            lines.append(
                f"  {label}: n={dist['n']}, median={dist['median'] / 60:.1f}m, "
                f"p90={dist['p90'] / 60:.1f}m, mean={dist['mean'] / 60:.1f}m"
            )
    corr = orient["roadmap_bytes_vs_claim_spearman"]
    if corr["rho"] is not None:
        lines.append(f"  roadmap bytes vs claim time: Spearman rho={corr['rho']:.2f} (n={corr['n']}; exploratory)")
    corr = orient["prior_local_sessions_in_area_vs_claim_spearman"]
    if corr["rho"] is not None:
        lines.append(
            f"  prior local sessions in area vs claim time: Spearman rho={corr['rho']:.2f} (n={corr['n']}; exploratory)"
        )
    infra = report["infrastructure"]
    lines.append("")
    lines.append(
        "CI model: "
        f"{infra['ci']['runner_minutes_per_merged_pr']:.1f} runner-min/merged PR, "
        f"{infra['ci']['vcpu_hours_per_merged_pr']:.2f} vCPU-h, "
        f"${infra['ci']['equivalent_usd_per_merged_pr']:.2f} equivalent compute"
    )
    decomposition = infra["ci"]["decomposition"]
    lines.append(
        "  proxy decomposition: "
        f"fixed={decomposition['fixed']['runner_minutes_per_merged_pr']:.1f}, "
        f"touched-code={decomposition['touched']['runner_minutes_per_merged_pr']:.1f}, "
        f"repository-wide={decomposition['repository']['runner_minutes_per_merged_pr']:.1f} runner-min/merged PR"
    )
    cache = infra["r2_complete_cache_fetch"]
    lines.append(
        f"R2 full cache fetch: {cache['objects']:,} objects, {cache['gib']:.3f} GiB, "
        f"${cache['gross_class_b_usd']:.4f} gross reads, $0 R2 egress"
    )
    lines.append(
        f"  CI multiplier: {cache['fetches_per_merged_pr']:.2f} fetches/merged PR = "
        f"{cache['gib_per_merged_pr']:.3f} GiB and ${cache['gross_class_b_usd_per_merged_pr']:.4f} gross reads"
    )
    if "monthly_at_merged_prs" in cache:
        lines.append(
            f"  at {cache['monthly_at_merged_prs']:g} merged PRs/month: "
            f"{cache['monthly_gib'] / 1024:.2f} TiB, "
            f"${cache['monthly_class_b_usd_after_free_tier']:.2f}/month reads after the free tier"
        )
    storage = infra["r2_storage"]
    lines.append(
        f"R2 retained storage estimate: {storage['estimated_retained_gib']:.2f} GiB, "
        f"${storage['gross_usd_month']:.3f}/month gross, "
        f"${storage['usd_month_after_10_gb_free_tier']:.2f}/month after the Standard free tier"
    )
    loc = report.get("loc_cost")
    if loc:
        components = loc["current_usd_per_changed_loc"]
        lines.append("")
        lines.append("LOC cost at the reference repository size:")
        lines.append(
            f"  N={loc['reference_repo_loc']:.3e}: ${components['total']:.3e}/changed LOC "
            f"(${components['total'] * loc['changed_per_net_loc']:.3e}/net retained LOC)"
        )
        lines.append(
            "  changed-LOC split: "
            f"AI=${components['ai_preparation_review_revision']:.3e}, "
            f"fixed CI=${components['ci_fixed']:.3e}, "
            f"touched-code CI=${components['ci_touched']:.3e}, "
            f"repository CI+R2=${components['ci_repository'] + components['r2_repository']:.3e}"
        )
        scaling = loc["best_scaling_scenario"]
        lines.append("")
        lines.append("LOC scaling scenario (N = existing repository LOC):")
        lines.append(
            f"  C(N) = ${scaling['marginal_intercept_usd']:.3e} + "
            f"${scaling['marginal_slope_usd_per_existing_loc']:.3e} N"
        )
        lines.append(
            f"  T_retained(N) = ${scaling['retained_integrated_linear_usd']:.3e} N + "
            f"${scaling['retained_integrated_quadratic_usd']:.3e} N^2"
        )
        lines.append("  target retained LOC    no-churn baseline    retained-growth total")
        for projection in loc["projections"]:
            lines.append(
                f"  {projection['repository_loc']:.3e}    "
                f"${projection['changed_loc']['no_churn_cumulative_usd']:.3e}             "
                f"${projection['net_retained_loc']['cumulative_usd']:.3e}"
            )
        upper = loc["all_infrastructure_scales_scenario"]
        lines.append(
            "  all-infrastructure-scales comparison: "
            f"C(N) = ${upper['marginal_intercept_usd']:.3e} + "
            f"${upper['marginal_slope_usd_per_existing_loc']:.3e} N"
        )
        lines.append(f"  caveat: {loc['caveat']}")
    return "\n".join(lines)

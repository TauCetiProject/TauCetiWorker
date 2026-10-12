"""Readable, bounded transcripts for structured authoring-agent event streams."""

from __future__ import annotations

import json
from typing import Any

TOOL_INPUT_LIMIT = 4 * 1024
TOOL_RESULT_LIMIT = 64 * 1024


def _json(value: Any, *, pretty: bool = True) -> str:
    separators = None if pretty else (",", ":")
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            indent=2 if pretty else None,
            separators=separators,
            sort_keys=True,
            default=str,
        )
    except (TypeError, ValueError):
        return repr(value)


def _utf8_prefix(text: str, limit: int) -> str:
    """Keep a UTF-8-safe prefix and report the exact number of omitted bytes."""
    raw = text.encode("utf-8", errors="replace")
    if len(raw) <= limit:
        return text
    prefix = raw[:limit].decode("utf-8", errors="ignore")
    kept = len(prefix.encode("utf-8"))
    return f"{prefix}\n[... omitted {len(raw) - kept} bytes ...]"


def _utf8_head_tail(text: str, limit: int) -> str:
    """Keep UTF-8-safe equal-sized ends and report the exact omitted byte count."""
    raw = text.encode("utf-8", errors="replace")
    if len(raw) <= limit:
        return text
    head_target = limit // 2
    tail_target = limit - head_target
    head = raw[:head_target].decode("utf-8", errors="ignore")
    tail = raw[-tail_target:].decode("utf-8", errors="ignore")
    kept = len(head.encode("utf-8")) + len(tail.encode("utf-8"))
    return f"{head}\n[... omitted {len(raw) - kept} bytes ...]\n{tail}"


def _section(label: str, body: str | None = None) -> str:
    if not body:
        return f"[{label}]\n"
    return f"[{label}]\n{body.rstrip()}\n"


def _line(label: str, detail: str | None = None) -> str:
    return f"[{label}]{' ' + detail if detail else ''}\n"


def _byte_len(value: Any) -> int:
    text = value if isinstance(value, str) else _json(value, pretty=False)
    return len(text.encode("utf-8", errors="replace"))


def _diagnostic_value(value: Any) -> Any:
    """Redact private-reasoning fields before rendering an unfamiliar event."""
    if isinstance(value, list):
        return [_diagnostic_value(item) for item in value]
    if not isinstance(value, dict):
        return value

    block_type = str(value.get("type", "")).lower()
    if block_type in {"thinking", "redacted_thinking"}:
        return {"type": block_type, "content": "[omitted]"}

    redacted: dict[str, Any] = {}
    for key, item in value.items():
        if key.lower() in {"thinking", "reasoning_content", "encrypted_content", "signature"}:
            redacted[key] = "[omitted]"
        else:
            redacted[key] = _diagnostic_value(item)
    return redacted


def _diagnostic_json(value: Any) -> str:
    try:
        return _json(_diagnostic_value(value), pretty=False)
    except Exception:
        return "[unrenderable event omitted]"


def _command_input(command: str) -> str:
    """Render a shell command without reproducing an embedded apply_patch body."""
    if "apply_patch" in command and "*** Begin Patch" in command:
        paths = [
            line.strip()
            for line in command.splitlines()
            if line.lstrip().startswith(("*** Add File:", "*** Update File:", "*** Delete File:"))
        ]
        summary = "apply_patch (body omitted)"
        if paths:
            summary += "\n" + "\n".join(f"- {path}" for path in paths)
        return _utf8_prefix(summary, TOOL_INPUT_LIMIT)
    return _utf8_prefix(command, TOOL_INPUT_LIMIT)


def _tool_input(name: str, value: Any) -> str:
    """Render tool input without reproducing file bodies or patches."""
    if not isinstance(value, dict):
        text = value if isinstance(value, str) else _json(value)
        return _utf8_prefix(text, TOOL_INPUT_LIMIT)

    kind = name.rsplit("__", 1)[-1].lower()
    if kind in {
        "edit",
        "write",
        "multiedit",
        "multi_edit",
        "notebookedit",
        "notebook_edit",
        "applypatch",
        "apply_patch",
    }:
        summary: dict[str, Any] = {}
        for key in ("file_path", "path", "notebook_path", "cell_id", "edit_mode", "replace_all"):
            if key in value:
                summary[key] = value[key]
        for key in ("old_string", "new_string", "content", "new_source", "patch"):
            if key in value:
                summary[f"{key}_bytes"] = _byte_len(value[key])
        edits = value.get("edits")
        if isinstance(edits, list):
            summary["edit_count"] = len(edits)
            summary["edits"] = [
                {
                    "old_string_bytes": _byte_len(edit.get("old_string", "")),
                    "new_string_bytes": _byte_len(edit.get("new_string", "")),
                    **({"replace_all": edit["replace_all"]} if "replace_all" in edit else {}),
                }
                for edit in edits
                if isinstance(edit, dict)
            ]
        return _utf8_prefix(_json(summary), TOOL_INPUT_LIMIT)

    return _utf8_prefix(_json(value), TOOL_INPUT_LIMIT)


def _content_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and item.get("type") == "text" and isinstance(item.get("text"), str):
                parts.append(item["text"])
            elif isinstance(item, dict) and item.get("type") in {"image", "audio"}:
                parts.append(f"[{item['type']} result omitted]")
            else:
                parts.append(_json(item))
        return "\n".join(part for part in parts if part)
    if isinstance(value, dict) and value.get("type") == "text" and isinstance(value.get("text"), str):
        return value["text"]
    return _json(value)


class AgentTranscriptRenderer:
    """Turn Codex/Claude JSONL into one stable, human-readable transcript.

    The renderer is deliberately line-oriented so the same instance works for a host CLI and for a
    Bubble command whose plain-text bootstrap output precedes the inner agent's JSONL stream.
    """

    def __init__(self, provider: str):
        self.provider = provider
        self.active = False
        self._tools: dict[str, str] = {}
        self._started: set[str] = set()
        self._last_assistant: str | None = None
        self._last_plan: tuple[tuple[str, bool], ...] | None = None
        self.saw_work = False
        self.terminal_failure_text: str | None = None
        self.terminal_failure_final = False
        self.compactions = 0
        self.usage: dict[str, int] = {}

    def render_line(self, raw: str) -> str:
        if self.provider not in {"codex", "claude"}:
            return raw
        stripped = raw.strip()
        if not stripped.startswith("{"):
            return raw
        try:
            event = json.loads(stripped)
        except Exception:
            return raw
        if not isinstance(event, dict):
            return raw

        kind, subtype = event.get("type"), event.get("subtype")
        if kind in {"context.compacted", "context_compacted", "thread.compacted"} or (
            kind == "system" and subtype == "compact_boundary"
        ):
            self.active = True
            self.compactions += 1
            return _line("compaction", f"{self.provider}; count={self.compactions}")
        if kind in {"turn.completed", "result"} and isinstance(event.get("usage"), dict):
            self.usage = {
                key: value for key, value in event["usage"].items()
                if key in {"input_tokens", "output_tokens", "cached_input_tokens", "reasoning_output_tokens",
                           "cache_read_input_tokens", "cache_creation_input_tokens"} and type(value) is int
            }

        try:
            rendered = self._render_codex(event) if self.provider == "codex" else self._render_claude(event)
        except Exception:
            if self.active:
                return _line(
                    f"{self.provider} malformed event",
                    _utf8_prefix(_diagnostic_json(event), TOOL_INPUT_LIMIT),
                )
            return raw
        if rendered is not None:
            return rendered
        if not self.active:
            return raw
        kind = str(event.get("type", "unknown"))
        subtype = event.get("subtype")
        if subtype:
            kind += f"/{subtype}"
        return _line(f"{self.provider} event {kind}", _utf8_prefix(_diagnostic_json(event), TOOL_INPUT_LIMIT))

    def _render_codex(self, event: dict[str, Any]) -> str | None:
        kind = event.get("type")
        if kind == "thread.started" and isinstance(event.get("thread_id"), str):
            self.active = True
            return _line("session", f"codex {event['thread_id']}")
        if kind == "turn.started":
            self.active = True
            return _line("turn", "started")
        if kind == "turn.completed":
            self.active = True
            usage = event.get("usage") if isinstance(event.get("usage"), dict) else {}
            fields = [
                f"input={usage[key]}" if key == "input_tokens" else f"{key.removesuffix('_tokens')}={usage[key]}"
                for key in ("input_tokens", "cached_input_tokens", "output_tokens", "reasoning_output_tokens")
                if isinstance(usage.get(key), int)
            ]
            return _line("turn", "completed" + (f"; {', '.join(fields)}" if fields else ""))
        if kind == "turn.failed":
            self.active = True
            error = event.get("error")
            message = error.get("message") if isinstance(error, dict) else None
            self.terminal_failure_text = message if isinstance(message, str) else _json(error)
            self.terminal_failure_final = True
            return _section("error", self.terminal_failure_text)
        if kind == "error":
            self.active = True
            self.terminal_failure_text = str(event.get("message", "unknown Codex error"))
            self.terminal_failure_final = True
            return _section("error", self.terminal_failure_text)
        if kind in {"item.started", "item.updated", "item.completed"} and isinstance(event.get("item"), dict):
            self.active = True
            return self._codex_item(kind, event["item"])
        return None

    def _codex_item(self, event_kind: str, item: dict[str, Any]) -> str:
        item_kind = item.get("type")
        item_id = str(item.get("id", ""))
        if item_kind in {
            "agent_message",
            "reasoning",
            "command_execution",
            "file_change",
            "mcp_tool_call",
            "collab_tool_call",
            "web_search",
            "todo_list",
        }:
            self.saw_work = True

        if item_kind == "agent_message" and event_kind == "item.completed":
            text = item.get("text")
            if isinstance(text, str):
                self._last_assistant = text
                return _section("assistant", text)
            return ""
        if item_kind == "agent_message":
            return ""
        if item_kind == "reasoning" and event_kind == "item.completed":
            text = item.get("text")
            return _section("reasoning", text) if isinstance(text, str) and text.strip() else ""
        if item_kind == "reasoning":
            return ""
        if item_kind == "command_execution":
            command = str(item.get("command", ""))
            if event_kind == "item.started":
                if item_id:
                    self._started.add(item_id)
                return _section("tool shell", _command_input(command))
            if event_kind != "item.completed":
                return ""
            out = ""
            if item_id not in self._started and command:
                out += _section("tool shell", _command_input(command))
            status = str(item.get("status", "completed"))
            exit_code = item.get("exit_code")
            detail = status + (f", exit {exit_code}" if isinstance(exit_code, int) else "")
            output = item.get("aggregated_output")
            body = _utf8_head_tail(output, TOOL_RESULT_LIMIT) if isinstance(output, str) and output else None
            return out + _section(f"tool result shell ({detail})", body)
        if item_kind == "file_change" and event_kind == "item.completed":
            status = str(item.get("status", "completed"))
            changes = item.get("changes") if isinstance(item.get("changes"), list) else []
            lines = [
                f"- {change.get('kind', 'update')} {change.get('path', '?')}"
                for change in changes
                if isinstance(change, dict)
            ]
            return _section(f"files ({status})", "\n".join(lines) if lines else "no paths reported")
        if item_kind == "mcp_tool_call":
            name = f"mcp {item.get('server', '?')}/{item.get('tool', '?')}"
            if event_kind == "item.started":
                if item_id:
                    self._started.add(item_id)
                return _section(f"tool {name}", _tool_input(name, item.get("arguments", {})))
            if event_kind != "item.completed":
                return ""
            out = ""
            if item_id not in self._started:
                out += _section(f"tool {name}", _tool_input(name, item.get("arguments", {})))
            error = item.get("error")
            if isinstance(error, dict) and isinstance(error.get("message"), str):
                body = error["message"]
            else:
                body = _content_text(item.get("result"))
            status = str(item.get("status", "completed"))
            return out + _section(f"tool result {name} ({status})", _utf8_head_tail(body, TOOL_RESULT_LIMIT))
        if item_kind == "collab_tool_call":
            name = f"collab {item.get('tool', '?')}"
            if event_kind == "item.started":
                if item_id:
                    self._started.add(item_id)
                prompt = item.get("prompt")
                return (
                    _section(f"tool {name}", _utf8_prefix(prompt, TOOL_INPUT_LIMIT))
                    if prompt
                    else _line(f"tool {name}")
                )
            if event_kind != "item.completed":
                return ""
            states = item.get("agents_states")
            body = _json(states) if states else ""
            return _section(
                f"tool result {name} ({item.get('status', 'completed')})",
                _utf8_head_tail(body, TOOL_RESULT_LIMIT),
            )
        if item_kind == "web_search":
            if event_kind == "item.started":
                if item_id:
                    self._started.add(item_id)
                return _line("web search", str(item.get("query", "")))
            if event_kind == "item.completed" and item_id not in self._started:
                return _line("web search", str(item.get("query", "")))
            return ""
        if item_kind == "todo_list":
            return self._plan(item.get("items"))
        if item_kind == "error" and event_kind == "item.completed":
            self.terminal_failure_text = str(item.get("message", "unknown Codex item error"))
            self.terminal_failure_final = True
            return _section("error", self.terminal_failure_text)
        if event_kind in {"item.started", "item.updated"}:
            return ""
        return _line(
            f"codex item {item_kind or 'unknown'}",
            _utf8_prefix(_diagnostic_json(item), TOOL_INPUT_LIMIT),
        )

    def _plan(self, raw_items: Any) -> str:
        if not isinstance(raw_items, list):
            return ""
        items = tuple(
            (str(item.get("text", "")), bool(item.get("completed"))) for item in raw_items if isinstance(item, dict)
        )
        if items == self._last_plan:
            return ""
        self._last_plan = items
        return _section("plan", "\n".join(f"- [{'x' if done else ' '}] {text}" for text, done in items))

    def _render_claude(self, event: dict[str, Any]) -> str | None:
        kind = event.get("type")
        subtype = event.get("subtype")
        if kind == "system" and subtype == "init":
            self.active = True
            detail = f"claude {event.get('session_id', '?')}"
            if event.get("model"):
                detail += f" model={event['model']}"
            if event.get("claude_code_version"):
                detail += f" version={event['claude_code_version']}"
            return _line("session", detail)
        if kind == "system" and subtype == "api_retry":
            self.active = True
            attempt = f"attempt {event.get('attempt', '?')}/{event.get('max_retries', '?')}"
            delay = event.get("retry_delay_ms")
            detail = attempt + (f" in {delay}ms" if isinstance(delay, int) else "")
            error = str(event.get("error", "provider error"))
            status = event.get("error_status")
            detail += f": {error}" + (f" (HTTP {status})" if isinstance(status, int) else "")
            if isinstance(status, int):
                self.terminal_failure_text = f"API Error: {status} {error}"
            return _line("retry", detail)
        if kind == "system" and subtype == "thinking_tokens":
            self.active = True
            return ""
        if kind == "rate_limit_event":
            self.active = True
            info = event.get("rate_limit_info") if isinstance(event.get("rate_limit_info"), dict) else {}
            details = [
                f"{label}={info[key]}"
                for key, label in (("status", "status"), ("rateLimitType", "type"), ("resetsAt", "resets"))
                if info.get(key) is not None
            ]
            return _line("rate limit", " ".join(details)) if details else ""
        if kind == "stream_event":
            # Partial token events are not requested and can include raw thinking deltas. A complete
            # assistant message follows, so suppress them defensively if provider config enables them.
            self.active = True
            return ""
        if kind == "assistant" and isinstance(event.get("message"), dict):
            self.active = True
            return self._claude_assistant(event)
        if kind == "user" and isinstance(event.get("message"), dict):
            self.active = True
            return self._claude_user(event)
        if kind == "result":
            self.active = True
            return self._claude_result(event)
        if kind == "system" and subtype == "plugin_install":
            self.active = True
            return _line("plugin install", f"{event.get('status', '?')} {event.get('name', '')}".rstrip())
        return None

    @staticmethod
    def _scope(label: str, parent: Any) -> str:
        return f"subagent {parent} {label}" if isinstance(parent, str) and parent else label

    def _claude_assistant(self, event: dict[str, Any]) -> str:
        message = event["message"]
        content = message.get("content")
        blocks = content if isinstance(content, list) else [content]
        parent = event.get("parent_tool_use_id")
        rendered: list[str] = []
        emitted_text = False
        for block in blocks:
            if isinstance(block, str):
                text = block
                block_kind = "text"
            elif isinstance(block, dict):
                block_kind = block.get("type")
                text = block.get("text")
            else:
                continue
            if block_kind == "text" and isinstance(text, str) and text.strip():
                self.saw_work = True
                emitted_text = True
                if not parent:
                    self._last_assistant = text
                rendered.append(_section(self._scope("assistant", parent), text))
            elif block_kind == "tool_use" and isinstance(block, dict):
                self.saw_work = True
                name = str(block.get("name", "unknown"))
                tool_id = str(block.get("id", ""))
                self._tools[tool_id] = name
                self._started.add(tool_id)
                rendered.append(
                    _section(self._scope(f"tool {name}", parent), _tool_input(name, block.get("input", {})))
                )
            # `thinking` and `redacted_thinking` are intentionally not transcript content.
        error = event.get("error")
        if error and not emitted_text:
            rendered.append(_line(self._scope("error", parent), f"Claude assistant: {error}"))
        return "".join(rendered)

    def _claude_user(self, event: dict[str, Any]) -> str:
        content = event["message"].get("content")
        blocks = content if isinstance(content, list) else [content]
        parent = event.get("parent_tool_use_id")
        rendered: list[str] = []
        for block in blocks:
            if not isinstance(block, dict) or block.get("type") != "tool_result":
                continue
            tool_id = str(block.get("tool_use_id", ""))
            self.saw_work = True
            name = self._tools.get(tool_id, tool_id or "unknown")
            status = "failed" if block.get("is_error") else "completed"
            body = _content_text(block.get("content"))
            rendered.append(
                _section(
                    self._scope(f"tool result {name} ({status})", parent),
                    _utf8_head_tail(body, TOOL_RESULT_LIMIT),
                )
            )
        return "".join(rendered)

    def _claude_result(self, event: dict[str, Any]) -> str:
        subtype = str(event.get("subtype", "unknown"))
        turns = event.get("num_turns")
        duration = event.get("duration_ms")
        detail = subtype
        if isinstance(turns, int):
            detail += f"; turns={turns}"
        if isinstance(duration, int):
            detail += f"; duration={duration}ms"
        rendered = [_line("result", detail)]
        if self.usage:
            rendered.append(_line("usage", "; ".join(f"{key}={value}" for key, value in self.usage.items())))

        result = event.get("result")
        if isinstance(result, str) and result.strip() and result.strip() != (self._last_assistant or "").strip():
            self._last_assistant = result
            rendered.insert(0, _section("assistant", result))

        if subtype != "success" or event.get("is_error"):
            errors = event.get("errors")
            messages = [str(value) for value in errors] if isinstance(errors, list) else []
            if isinstance(result, str) and result.strip() and result not in messages:
                messages.append(result)
            status = event.get("api_error_status")
            if isinstance(status, int) and not any("API Error:" in message for message in messages):
                messages.append(f"API Error: {status}")
            if not messages:
                messages.append(subtype)
            self.terminal_failure_text = "\n".join(messages)
            self.terminal_failure_final = True
            rendered.append(_section("error", self.terminal_failure_text))
        else:
            self.terminal_failure_text = None
            self.terminal_failure_final = False
        return "".join(rendered)


__all__ = ["AgentTranscriptRenderer", "TOOL_INPUT_LIMIT", "TOOL_RESULT_LIMIT"]

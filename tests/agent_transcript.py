#!/usr/bin/env python3
"""Structured Codex/Claude events become readable, bounded transcripts."""

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
import tauceti_worker as tc
from tauceti_worker.transcript import TOOL_INPUT_LIMIT, TOOL_RESULT_LIMIT

fails = 0


def check(name, condition):
    global fails
    ok = bool(condition)
    print(f"[{'OK ' if ok else 'XX '}] {name}")
    fails += not ok


def event(renderer, value):
    return renderer.render_line(json.dumps(value, ensure_ascii=False) + "\n")


# Captured from Claude 2.1.316 against a local API returning a credit-balance 400.
# The assistant is synthetic and the final subtype says "success", despite exit 1.
credit = tc.AgentTranscriptRenderer("claude")
credit_out = "".join(
    credit.render_line(line)
    for line in (REPO / "tests/fixtures/claude_credit_refusal_2_1_316.jsonl").read_text().splitlines(keepends=True)
)
check("synthetic credit refusal remains visible", "Credit balance is too low" in credit_out)
check("synthetic refusal is not structured work", not credit.saw_work)
check(
    "credit refusal has terminal HTTP evidence", credit.terminal_failure_final and "400" in credit.terminal_failure_text
)


# Captured from a real Codex 0.146 `exec --json` smoke call. Replaying the provider output guards the
# top-level envelope as well as the individual hand-built edge cases below.
captured_codex = tc.AgentTranscriptRenderer("codex")
captured_out = "".join(
    captured_codex.render_line(line)
    for line in (REPO / "tests/fixtures/codex_exec_0_146.jsonl").read_text().splitlines(keepends=True)
)
check(
    "captured Codex JSONL schema is normalized",
    captured_out == "[session] codex 019fd20e-dff0-7a11-a1c1-ae32a3f35a60\n"
    "[turn] started\n"
    "[assistant]\nOK\n"
    "[turn] completed; input=15036, cached_input=9984, output=5, reasoning_output=0\n",
)


# Plain Bubble bootstrap output and unrelated JSON remain byte-for-byte intact before the agent starts.
codex = tc.AgentTranscriptRenderer("codex")
check("plain bootstrap output passes through", codex.render_line("lake build: ok\n") == "lake build: ok\n")
check("pre-agent JSON passes through", codex.render_line('{"build":"ok"}\n') == '{"build":"ok"}\n')

out = event(codex, {"type": "thread.started", "thread_id": "codex-session"})
check("Codex session is readable", out == "[session] codex codex-session\n")
out = event(
    codex,
    {"type": "item.completed", "item": {"id": "r1", "type": "reasoning", "text": "Inspecting the fix."}},
)
check("Codex reasoning summary is retained", "[reasoning]\nInspecting the fix." in out)
out = event(
    codex,
    {
        "type": "item.started",
        "item": {"id": "r2", "type": "reasoning", "encrypted_content": "PRIVATE REASONING"},
    },
)
check("Codex reasoning start payload is suppressed", out == "" and "PRIVATE REASONING" not in out)

out = event(
    codex,
    {
        "type": "item.started",
        "item": {
            "id": "c1",
            "type": "command_execution",
            "command": "lake build",
            "aggregated_output": "",
            "exit_code": None,
            "status": "in_progress",
        },
    },
)
check("Codex command invocation is retained", out == "[tool shell]\nlake build\n")
big_result = "HEAD\n" + "x" * (TOOL_RESULT_LIMIT + 123) + "\nTAIL"
out = event(
    codex,
    {
        "type": "item.completed",
        "item": {
            "id": "c1",
            "type": "command_execution",
            "command": "lake build",
            "aggregated_output": big_result,
            "exit_code": 0,
            "status": "completed",
        },
    },
)
omitted = len(big_result.encode()) - TOOL_RESULT_LIMIT
check("Codex command result keeps status", "[tool result shell (completed, exit 0)]" in out)
check("large result keeps both ends", "HEAD" in out and out.rstrip().endswith("TAIL"))
check("large result reports exact omitted bytes", f"[... omitted {omitted} bytes ...]" in out)

patch_command = """apply_patch <<'PATCH'
*** Begin Patch
*** Update File: TauCeti/A.lean
@@
-old private diff
+new private diff
*** End Patch
PATCH"""
out = event(
    codex,
    {
        "type": "item.started",
        "item": {
            "id": "c2",
            "type": "command_execution",
            "command": patch_command,
            "status": "in_progress",
        },
    },
)
check("Codex apply_patch body is suppressed", "TauCeti/A.lean" in out and "private diff" not in out)

out = event(
    codex,
    {
        "type": "item.completed",
        "item": {
            "id": "f1",
            "type": "file_change",
            "changes": [
                {"path": "TauCeti/A.lean", "kind": "update"},
                {"path": "TauCeti/B.lean", "kind": "add"},
            ],
            "status": "completed",
        },
    },
)
check("Codex file changes list actions and paths", "- update TauCeti/A.lean" in out and "- add TauCeti/B.lean" in out)
check("Codex file changes contain no diff", "diff --git" not in out and "@@" not in out)

out = event(
    codex,
    {
        "type": "item.started",
        "item": {
            "id": "m1",
            "type": "mcp_tool_call",
            "server": "github",
            "tool": "get_issue",
            "arguments": {"number": 131},
            "result": None,
            "error": None,
            "status": "in_progress",
        },
    },
)
check("Codex MCP call includes tool and input", "[tool mcp github/get_issue]" in out and '"number": 131' in out)
out = event(
    codex,
    {
        "type": "item.completed",
        "item": {
            "id": "m1",
            "type": "mcp_tool_call",
            "server": "github",
            "tool": "get_issue",
            "arguments": {"number": 131},
            "result": {"content": [{"type": "text", "text": "issue body"}]},
            "error": None,
            "status": "completed",
        },
    },
)
check("Codex MCP result is retained", "[tool result mcp github/get_issue (completed)]" in out and "issue body" in out)

plan = {
    "type": "item.updated",
    "item": {"id": "p1", "type": "todo_list", "items": [{"text": "Test", "completed": True}]},
}
check("Codex plan is readable", "- [x] Test" in event(codex, plan))
check("unchanged Codex plan is deduplicated", event(codex, plan) == "")
unknown = event(codex, {"type": "future.codex.event", "payload": "z" * 10000})
check(
    "unknown Codex events are bounded diagnostics",
    unknown.startswith("[codex event future.codex.event]") and len(unknown) < 5000,
)
check("malformed JSON is not lost", codex.render_line("{broken\n") == "{broken\n")


claude = tc.AgentTranscriptRenderer("claude")
check("Claude pre-agent JSON passes through", claude.render_line('{"cache":"hit"}\n') == '{"cache":"hit"}\n')
out = event(
    claude,
    {
        "type": "system",
        "subtype": "init",
        "session_id": "claude-session",
        "model": "opus",
        "claude_code_version": "2.1.219",
    },
)
check("Claude session metadata is readable", "[session] claude claude-session model=opus version=2.1.219" in out)

out = event(
    claude,
    {
        "type": "assistant",
        "session_id": "claude-session",
        "parent_tool_use_id": None,
        "message": {
            "content": [
                {"type": "thinking", "thinking": "hidden chain of thought"},
                {"type": "text", "text": "I will inspect the failing file."},
                {
                    "type": "tool_use",
                    "id": "tool-1",
                    "name": "Edit",
                    "input": {
                        "file_path": "TauCeti/A.lean",
                        "old_string": "SECRET OLD BODY",
                        "new_string": "SECRET NEW BODY",
                    },
                },
            ]
        },
    },
)
check("Claude narration is retained", "[assistant]\nI will inspect the failing file." in out)
check("Claude raw thinking is omitted", "hidden chain of thought" not in out)
check("Claude edit lists its path", "[tool Edit]" in out and "TauCeti/A.lean" in out)
check("Claude edit bodies are suppressed", "SECRET OLD BODY" not in out and "SECRET NEW BODY" not in out)
check("Claude edit reports body sizes", '"old_string_bytes": 15' in out and '"new_string_bytes": 15' in out)

out = event(
    claude,
    {
        "type": "assistant",
        "session_id": "claude-session",
        "parent_tool_use_id": None,
        "message": {
            "content": [
                {
                    "type": "tool_use",
                    "id": "tool-2",
                    "name": "Bash",
                    "input": {"command": "x" * (TOOL_INPUT_LIMIT * 2)},
                }
            ]
        },
    },
)
check(
    "Claude generic tool input is bounded",
    "omitted" in out and len(out.encode()) < TOOL_INPUT_LIMIT + 256,
)

utf8_result = "é" + "🙂" * 20000 + "done"
out = event(
    claude,
    {
        "type": "user",
        "session_id": "claude-session",
        "parent_tool_use_id": None,
        "message": {
            "content": [{"type": "tool_result", "tool_use_id": "tool-1", "content": utf8_result, "is_error": False}]
        },
    },
)
check("Claude tool result is matched by id", "[tool result Edit (completed)]" in out)
check("UTF-8 result remains valid and keeps its tail", "�" not in out and out.rstrip().endswith("done"))
raw_utf8 = utf8_result.encode()
head = raw_utf8[: TOOL_RESULT_LIMIT // 2].decode(errors="ignore")
tail = raw_utf8[-(TOOL_RESULT_LIMIT - TOOL_RESULT_LIMIT // 2) :].decode(errors="ignore")
omitted_utf8 = len(raw_utf8) - len(head.encode()) - len(tail.encode())
check("UTF-8 result reports exact omission", f"omitted {omitted_utf8} bytes" in out)

final = "Implemented and verified."
assistant_final = event(
    claude,
    {
        "type": "assistant",
        "session_id": "claude-session",
        "parent_tool_use_id": None,
        "message": {"content": [{"type": "text", "text": final}]},
    },
)
result_final = event(
    claude,
    {
        "type": "result",
        "subtype": "success",
        "session_id": "claude-session",
        "is_error": False,
        "num_turns": 3,
        "duration_ms": 1200,
        "result": final,
    },
)
check("Claude final response is present once", (assistant_final + result_final).count(final) == 1)
check("Claude result metadata remains", "[result] success; turns=3; duration=1200ms" in result_final)

retry = event(
    claude,
    {
        "type": "system",
        "subtype": "api_retry",
        "session_id": "claude-session",
        "attempt": 1,
        "max_retries": 3,
        "retry_delay_ms": 500,
        "error_status": 529,
        "error": "overloaded",
    },
)
check("Claude retries are visible", retry == "[retry] attempt 1/3 in 500ms: overloaded (HTTP 529)\n")
check("Claude retry is not terminal evidence", not claude.terminal_failure_final)
thinking_tokens = event(
    claude,
    {
        "type": "system",
        "subtype": "thinking_tokens",
        "session_id": "claude-session",
        "estimated_tokens": 100,
    },
)
check("Claude thinking token counters are suppressed", thinking_tokens == "")
rate_limit = event(
    claude,
    {
        "type": "rate_limit_event",
        "session_id": "claude-session",
        "rate_limit_info": {"status": "allowed", "rateLimitType": "five_hour", "resetsAt": 123},
    },
)
check("Claude rate limit metadata is compact", rate_limit == "[rate limit] status=allowed type=five_hour resets=123\n")
partial = event(
    claude,
    {
        "type": "stream_event",
        "session_id": "claude-session",
        "event": {"delta": {"type": "thinking_delta", "thinking": "raw partial thought"}},
    },
)
check("Claude partial thinking events are suppressed", partial == "")
failure = event(
    claude,
    {
        "type": "result",
        "subtype": "error_during_execution",
        "session_id": "claude-session",
        "is_error": True,
        "num_turns": 0,
        "duration_ms": 50,
        "api_error_status": 529,
        "errors": ["Overloaded"],
    },
)
check("Claude fatal HTTP status keeps classifier shape", failure.rstrip().endswith("API Error: 529"))
check("Claude fatal result is terminal evidence", claude.terminal_failure_final)
unknown = event(claude, {"type": "future_claude_event", "session_id": "claude-session", "blob": "q" * 10000})
check(
    "unknown Claude events are bounded diagnostics",
    unknown.startswith("[claude event future_claude_event]") and len(unknown) < 5000,
)
unknown_thinking = event(
    claude,
    {
        "type": "future_claude_event",
        "thinking": "raw future thought",
        "content": [{"type": "redacted_thinking", "data": "private provider payload"}],
    },
)
check(
    "unknown Claude events redact private reasoning",
    "raw future thought" not in unknown_thinking
    and "private provider payload" not in unknown_thinking
    and "[omitted]" in unknown_thinking,
)

plain = tc.AgentTranscriptRenderer("deepseek")
check(
    "plain-text providers remain passthrough", plain.render_line('{"type":"assistant"}\n') == '{"type":"assistant"}\n'
)

print(f"\n{'PASS' if not fails else 'FAIL'}: {fails} failure(s)")
sys.exit(1 if fails else 0)

#!/usr/bin/env python3
"""M8: verify the host agent argv is byte-for-byte what round.sh's run_agent builds."""

import io
import os
import sys
import tempfile
from contextlib import redirect_stdout
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
import tauceti_worker as tc

P = "DO THE WORK"
fails = 0


def check(name, argv, expect):
    global fails
    ok = argv == expect
    print(f"[{'OK ' if ok else 'XX '}] {name}: {argv}")
    if not ok:
        print(f"      expected: {expect}")
        fails += 1


a, env = tc.host_agent_argv(P, "codex")
check(
    "codex",
    a,
    [
        "codex",
        "exec",
        "--json",
        "--model",
        "gpt-6-sol",
        "-c",
        'model_reasoning_effort="medium"',
        "-c",
        'model_reasoning_summary="detailed"',
        "-c",
        "show_raw_agent_reasoning=false",
        "--sandbox",
        "danger-full-access",
        "--skip-git-repo-check",
        P,
    ],
)
assert "OPENAI_API_KEY" not in env, "codex env must drop OPENAI_API_KEY (bills the ChatGPT plan)"
print("[OK ] codex env drops OPENAI_API_KEY")

a, env = tc.host_agent_argv(P, "claude")
check(
    "claude",
    a,
    [
        "claude",
        "-p",
        P,
        "--output-format",
        "stream-json",
        "--verbose",
        "--model",
        "claude-opus-5-5",
        "--effort",
        "medium",
        "--dangerously-skip-permissions",
    ],
)
assert "ANTHROPIC_API_KEY" not in env, "claude env must drop ANTHROPIC_API_KEY (bills the Max plan)"
print("[OK ] claude env drops ANTHROPIC_API_KEY")

a, env = tc.host_agent_argv(P, "deepseek")
check("deepseek", a, [tc.PI_RUN, "openrouter", tc.OPENROUTER_MODELS["deepseek"], "--prompt", P])

a, env = tc.host_agent_argv(P, "kiro")
check(
    "kiro",
    a,
    [
        "kiro-cli",
        "chat",
        "--no-interactive",
        "--trust-all-tools",
        "--model",
        "gpt-5.6-sol",
        "--effort",
        "high",
        P,
    ],
)
check("kiro model is explicit (never Auto)", "auto" in [arg.lower() for arg in a], False)

# PATH must prepend HERE so the agent resolves git-safe-push / gh-safe-pr-create / claim.sh
assert env["PATH"].startswith(str(tc.HERE / "scripts") + ":"), "PATH must prepend the repo dir"
print("[OK ] PATH prepends repo dir for the safe-push/claim wrappers")

# Agent prompts are always passed in argv. In Bubble, an inherited terminal crosses SSH as a non-TTY
# stream; Codex then waits for more prompt text until EOF. Both output modes must close stdin.
saved_popen = tc.agents.subprocess.Popen
saved_stream = os.environ.get("TAUCETI_STREAM")
saved_runtime_status = os.environ.get(tc.runtime_status.STATUS_ENV)
calls = []


class FakeProc:
    def __init__(self, output="", returncode=0):
        self.stdout = io.StringIO(output)
        self.returncode = returncode

    def wait(self):
        return self.returncode

    def kill(self):
        pass


tc.agents.subprocess.Popen = lambda *a, **k: calls.append(k) or FakeProc()
try:
    with tempfile.TemporaryDirectory() as td:
        os.environ["TAUCETI_STREAM"] = "1"
        tc.run_agent_proc(["agent"], env={}, logdir=Path(td), label="test", provider="deepseek")
        check("streamed agent stdin is closed", calls[-1].get("stdin"), tc.agents.subprocess.DEVNULL)
        os.environ.pop("TAUCETI_STREAM")
        tc.run_agent_proc(["agent"], env={}, logdir=Path(td), label="test", provider="deepseek")
        check("logged agent stdin is closed", calls[-1].get("stdin"), tc.agents.subprocess.DEVNULL)

        transcript_events = (
            "\n".join(
                [
                    "bubble bootstrap complete",
                    tc.json.dumps(
                        {
                            "type": "system",
                            "subtype": "init",
                            "session_id": "parity-session",
                            "model": "opus",
                        }
                    ),
                    tc.json.dumps(
                        {
                            "type": "assistant",
                            "session_id": "parity-session",
                            "parent_tool_use_id": None,
                            "message": {"content": [{"type": "text", "text": "working"}]},
                        }
                    ),
                    tc.json.dumps(
                        {
                            "type": "result",
                            "subtype": "success",
                            "session_id": "parity-session",
                            "is_error": False,
                            "num_turns": 1,
                            "result": "working",
                        }
                    ),
                ]
            )
            + "\n"
        )
        tc.agents.subprocess.Popen = lambda *_a, **_k: FakeProc(transcript_events)
        parity_dir = Path(td) / "parity"
        tc.run_agent_proc(["claude"], env={}, logdir=parity_dir, label="agent-claude", provider="claude")
        logged_transcript = next(parity_dir.glob("agent-claude-*.log")).read_text()
        os.environ["TAUCETI_STREAM"] = "1"
        streamed = io.StringIO()
        with redirect_stdout(streamed):
            tc.run_agent_proc(["claude"], env={}, logdir=parity_dir, label="agent-claude", provider="claude")
        os.environ.pop("TAUCETI_STREAM")
        check("logfile and --stream transcripts match", streamed.getvalue(), logged_transcript)
        check(
            "Bubble prelude survives normalization", logged_transcript.startswith("bubble bootstrap complete\n"), True
        )

        status_file = Path(td) / "status.json"
        os.environ[tc.runtime_status.STATUS_ENV] = str(status_file)

        def fail_with_diagnostic(*_args, **_kwargs):
            events = [
                {
                    "type": "system",
                    "subtype": "init",
                    "session_id": "failure-session",
                    "model": "opus",
                },
                *[
                    {
                        "type": "system",
                        "subtype": "api_retry",
                        "session_id": "failure-session",
                        "attempt": attempt,
                        "max_retries": 5,
                        "retry_delay_ms": 10,
                        "error_status": 529,
                        "error": "overloaded",
                    }
                    for attempt in range(1, 6)
                ],
                {
                    "type": "result",
                    "subtype": "error_during_execution",
                    "session_id": "failure-session",
                    "is_error": True,
                    "num_turns": 0,
                    "errors": ["Overloaded"],
                    "api_error_status": 529,
                },
            ]
            return FakeProc("\n".join(tc.json.dumps(item) for item in events) + "\n", returncode=1)

        tc.agents.subprocess.Popen = fail_with_diagnostic
        tc.run_agent_proc(["claude"], env={}, logdir=Path(td), label="agent-claude", provider="claude")
        failure = tc.runtime_status.read_json(status_file)
        check(
            "agent failure publishes its diagnostic",
            failure.get("failure_reason"),
            "claude agent exited with status 1: API Error: 529",
        )
        check("agent failure publishes its exit code", failure.get("failure_code"), 1)
        assert Path(failure["failure_log"]).name.startswith("agent-claude-")
        check(
            "structured retry envelope does not hide an infrastructure outage",
            tc.take_last_agent_infra_failure(),
            "provider returned 529",
        )

        def recovered_retry_then_transport_failure(*_args, **_kwargs):
            events = [
                {
                    "type": "system",
                    "subtype": "init",
                    "session_id": "recovered-retry",
                    "model": "opus",
                },
                {
                    "type": "system",
                    "subtype": "api_retry",
                    "session_id": "recovered-retry",
                    "attempt": 1,
                    "max_retries": 5,
                    "error_status": 529,
                    "error": "overloaded",
                },
            ]
            return FakeProc("\n".join(tc.json.dumps(item) for item in events) + "\n", returncode=1)

        tc.agents.subprocess.Popen = recovered_retry_then_transport_failure
        os.environ["TAUCETI_STREAM"] = "1"
        with redirect_stdout(io.StringIO()):
            tc.run_agent_proc(["claude"], env={}, logdir=Path(td), label="agent-claude", provider="claude")
        os.environ.pop("TAUCETI_STREAM")
        check("a recovered retry is not the terminal failure", tc.take_last_agent_infra_failure(), None)

        def worked_then_provider_failure(*_args, **_kwargs):
            events = [
                {"type": "system", "subtype": "init", "session_id": "worked", "model": "opus"},
                {
                    "type": "assistant",
                    "session_id": "worked",
                    "parent_tool_use_id": None,
                    "message": {"content": [{"type": "text", "text": "work began"}]},
                },
                {
                    "type": "result",
                    "subtype": "error_during_execution",
                    "session_id": "worked",
                    "is_error": True,
                    "api_error_status": 529,
                    "errors": ["Overloaded"],
                },
            ]
            return FakeProc("\n".join(tc.json.dumps(item) for item in events) + "\n", returncode=1)

        tc.agents.subprocess.Popen = worked_then_provider_failure
        tc.run_agent_proc(["claude"], env={}, logdir=Path(td), label="agent-claude", provider="claude")
        check("structured work prevents a false attempt refund", tc.take_last_agent_infra_failure(), None)
finally:
    tc.agents.subprocess.Popen = saved_popen
    if saved_stream is None:
        os.environ.pop("TAUCETI_STREAM", None)
    else:
        os.environ["TAUCETI_STREAM"] = saved_stream
    if saved_runtime_status is None:
        os.environ.pop(tc.runtime_status.STATUS_ENV, None)
    else:
        os.environ[tc.runtime_status.STATUS_ENV] = saved_runtime_status

# Host configuration must not change worker authoring. Both backends consume the
# committed/provider-specific profile instead.
saved_host_home = tc.agents._host_home
saved_model = os.environ.pop("TAUCETI_CODEX_MODEL", None)
try:
    with tempfile.TemporaryDirectory() as td:
        host_home = Path(td)
        (host_home / ".codex").mkdir()
        (host_home / ".codex" / "config.toml").write_text('model = "gpt-5.6-luna"\n')
        tc.agents._host_home = lambda: host_home
        check("Codex default ignores host model selection", tc.agents._codex_model(), "gpt-6-sol")
        (host_home / ".codex" / "config.toml").write_text("not valid [")
        check("invalid host model config is irrelevant", tc.agents._codex_model(), "gpt-6-sol")
        os.environ["TAUCETI_CODEX_MODEL"] = "operator-model"
        check("bubble Codex model operator override", tc.agents._codex_model(), "operator-model")
finally:
    tc.agents._host_home = saved_host_home
    if saved_model is None:
        os.environ.pop("TAUCETI_CODEX_MODEL", None)
    else:
        os.environ["TAUCETI_CODEX_MODEL"] = saved_model

# $TAUCETI_CLAUDE_CMD wraps/replaces the host claude executable; the standard flags are still appended,
# and an empty / whitespace-only value falls back to bare `claude` rather than a broken argv.
_saved = tc.agents.CLAUDE_CMD
tc.agents.CLAUDE_CMD = "my-wrapper --flag claude"
a, _ = tc.host_agent_argv(P, "claude")
check(
    "claude override",
    a,
    [
        "my-wrapper",
        "--flag",
        "claude",
        "-p",
        P,
        "--output-format",
        "stream-json",
        "--verbose",
        "--model",
        "claude-opus-5-5",
        "--effort",
        "medium",
        "--dangerously-skip-permissions",
    ],
)
tc.agents.CLAUDE_CMD = "   "
a, _ = tc.host_agent_argv(P, "claude")
check(
    "claude override blank falls back",
    a,
    [
        "claude",
        "-p",
        P,
        "--output-format",
        "stream-json",
        "--verbose",
        "--model",
        "claude-opus-5-5",
        "--effort",
        "medium",
        "--dangerously-skip-permissions",
    ],
)
tc.agents.CLAUDE_CMD = _saved
check(
    "Bubble Codex inner command",
    tc.agent_inner_cmd("codex"),
    "env OPENAI_API_KEY= ANTHROPIC_API_KEY= codex exec --json --model gpt-6-sol "
    "-c 'model_reasoning_effort=\"medium\"' -c 'model_reasoning_summary=\"detailed\"' "
    "-c show_raw_agent_reasoning=false --sandbox danger-full-access --skip-git-repo-check "
    '"$(cat /opt/round/prompt.txt)"',
)
check(
    "Bubble Claude inner command",
    tc.agent_inner_cmd("claude"),
    'env ANTHROPIC_API_KEY= OPENAI_API_KEY= CLAUDECODE= claude -p "$(cat /opt/round/prompt.txt)" '
    "--output-format stream-json --verbose --model claude-opus-5-5 --effort medium "
    "--dangerously-skip-permissions",
)

print(f"\n{'PASS' if not fails else 'FAIL'}: {fails} mismatch(es)")
sys.exit(1 if fails else 0)

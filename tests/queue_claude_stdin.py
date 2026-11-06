#!/usr/bin/env python3
"""Queue Bash stdin regression. Set TAUCETI_TEST_CLAUDE to a native CLI for the
production-launcher test; its scripted local API uses no inference or credentials.
"""

from __future__ import annotations

import contextlib
import http.server
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tauceti_queue import repair, repair_task
from tauceti_worker import agents
from tauceti_worker.repair_recovery import environment


class API(http.server.ThreadingHTTPServer):
    def __init__(self, commands):
        self.commands = commands
        self.calls = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
                if self.path.endswith("/count_tokens"):
                    data = json.dumps({"input_tokens": 10}).encode()
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(data)
                    return
                index = len(self.server.calls)
                main_request = self.path.split("?", 1)[0].endswith("/messages") and any(
                    tool.get("name") == "Bash" for tool in body.get("tools", [])
                )
                if main_request:
                    self.server.calls.append(
                        {
                            "path": self.path,
                            "model": body.get("model"),
                            "tool_names": [t.get("name") for t in body.get("tools", [])],
                            "last_message": next(
                                (m for m in reversed(body.get("messages", [])) if m.get("role") == "user"), {}
                            ),
                        }
                    )
                if main_request and index < len(self.server.commands) and self.server.commands[index] is not None:
                    content = {
                        "type": "tool_use",
                        "id": f"toolu_probe_{index}",
                        "name": "Bash",
                        "input": {
                            "command": self.server.commands[index],
                            "description": "isolated stdin probe",
                            "timeout": 120000,
                        },
                    }
                    stop = "tool_use"
                else:
                    content = {"type": "text", "text": "Probe complete."}
                    stop = "end_turn"
                message = {
                    "id": f"msg_probe_{index}",
                    "type": "message",
                    "role": "assistant",
                    "model": body.get("model"),
                    "content": [],
                    "stop_reason": None,
                    "stop_sequence": None,
                    "usage": {"input_tokens": 10, "output_tokens": 1},
                }
                events = [("message_start", {"type": "message_start", "message": message})]
                if content["type"] == "tool_use":
                    events += [
                        (
                            "content_block_start",
                            {"type": "content_block_start", "index": 0, "content_block": {**content, "input": {}}},
                        ),
                        (
                            "content_block_delta",
                            {
                                "type": "content_block_delta",
                                "index": 0,
                                "delta": {"type": "input_json_delta", "partial_json": json.dumps(content["input"])},
                            },
                        ),
                    ]
                else:
                    events += [
                        (
                            "content_block_start",
                            {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
                        ),
                        (
                            "content_block_delta",
                            {
                                "type": "content_block_delta",
                                "index": 0,
                                "delta": {"type": "text_delta", "text": content["text"]},
                            },
                        ),
                    ]
                events += [
                    ("content_block_stop", {"type": "content_block_stop", "index": 0}),
                    (
                        "message_delta",
                        {
                            "type": "message_delta",
                            "delta": {"stop_reason": stop, "stop_sequence": None},
                            "usage": {"output_tokens": 10},
                        },
                    ),
                    ("message_stop", {"type": "message_stop"}),
                ]
                payload = "".join(
                    "event: " + name + "\ndata: " + json.dumps(data) + "\n\n" for name, data in events
                ).encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                with contextlib.suppress(BrokenPipeError):
                    self.wfile.write(payload)

        super().__init__(("127.0.0.1", 0), Handler)


class ShellEnvironment(unittest.TestCase):
    def test_claude_and_api_own_only_their_session_settings(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.dict(os.environ, {"HOME": str(root), "CLAUDE_CONFIG_DIR": str(root / "operator")}, clear=True):
                for provider in ("claude", "api"):
                    with self.subTest(provider=provider):
                        changes = repair.isolation_env(root / "state", provider=provider, api_key="test-key")
                        config = Path(changes["CLAUDE_CONFIG_DIR"])
                        settings = (config / "settings.json").read_bytes()
                        with environment(**changes):
                            argv, _ = agents.host_agent_argv("test", "claude")
                        startup = Path(json.loads(argv[argv.index("--settings") + 1])["env"]["CLAUDE_ENV_FILE"])
                        self.assertEqual(startup.parent, config)
                        self.assertEqual(startup.read_text(), "exec </dev/null\n")
                        self.assertEqual((config / "settings.json").read_bytes(), settings)
                        self.assertFalse(list(config.glob(".bash-env-*")))
                changes = repair.isolation_env(root / "state", provider="codex")
                self.assertNotIn("TAUCETI_REPAIR_CLAUDE_ENV_FILE", changes)
                with environment(**changes):
                    argv, _ = agents.host_agent_argv("test", "codex")
                self.assertNotIn("--settings", argv)
                argv, _ = agents.host_agent_argv("test", "claude")
                self.assertNotIn("--settings", argv)

    def test_failed_atomic_replace_preserves_the_previous_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp)
            startup = repair.prepare_claude_shell_env(config)
            with patch.object(repair.os, "replace", side_effect=OSError("replace failed")):
                with self.assertRaisesRegex(OSError, "replace failed"):
                    repair.prepare_claude_shell_env(config)
            self.assertEqual(startup.read_text(), "exec </dev/null\n")
            self.assertEqual(list(config.iterdir()), [startup])

    @unittest.skipUnless(os.environ.get("TAUCETI_TEST_CLAUDE"), "set TAUCETI_TEST_CLAUDE for native CLI regression")
    def test_production_launcher_cwd_resume_and_settings_precedence(self):
        binary = shutil.which(os.environ["TAUCETI_TEST_CLAUDE"])
        self.assertIsNotNone(binary)
        with tempfile.TemporaryDirectory(prefix="queue-stdin-") as tmp:
            root = Path(tmp)
            home = root / "home"
            home.mkdir()
            checkout = root / "checkout"
            checkout.mkdir()
            (checkout / "subdir").mkdir()
            # Deliberately conflicting inherited, private user and project settings.
            broken = str(root / "missing-env.sh")
            project = checkout / ".claude"
            project.mkdir()
            (project / "settings.json").write_text(json.dumps({"env": {"CLAUDE_ENV_FILE": broken}}))
            commands = [
                "cat <<'END'\nheredoc payload\nEND\ngrep -c exec $(ls /nonexistent-rebase-done 2>/dev/null); test $? -eq 1 && echo EOF_REACHED",
                "cd subdir; cat <<'END'\ncwd payload\nEND\ngrep -c exec; test $? -eq 1 && echo EOF_REACHED",
                "pwd; printf 'exec\nother\n' | grep -c exec; cat <<'END'\nexplicit heredoc\nEND\n"
                "printf 'exec\n' > input.txt; grep -c exec < input.txt; "
                "grep -c exec < <(printf 'exec\n'); exec 3< input.txt; cat <&3; "
                "grep -c exec; test $? -eq 1 && echo EOF_REACHED",
            ]
            api = API(commands)
            thread = threading.Thread(target=api.serve_forever, daemon=True)
            thread.start()
            self.addCleanup(api.server_close)
            self.addCleanup(api.shutdown)
            isolated = {key: os.environ[key] for key in ("PATH", "LANG", "USER") if key in os.environ}
            isolated.update(
                HOME=str(home),
                CLAUDE_CONFIG_DIR=str(home / ".claude"),
                CLAUDE_ENV_FILE=broken,
                ANTHROPIC_BASE_URL=f"http://127.0.0.1:{api.server_port}",
                DISABLE_AUTOUPDATER="1",
                CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1",
                IS_SANDBOX="1",  # the native CLI regression also runs in the CI container
                TAUCETI_ACTIVITY_DIR=str(root / "activity"),
                TAUCETI_FLEET_EVENT_DIR=str(root / "events"),
            )
            cfg = repair_task.RunnerConfig(
                service="http://unused.invalid",
                clone=checkout,
                root=root / "state",
                api_key="local-test-key",
                agent_timeout=45,
                inference_attempts=1,
                providers=("api",),
            )
            task = repair_task.Task.from_json(
                dict(
                    task_id="stdin-test",
                    kind="repair",
                    repository="TauCetiProject/TauCeti",
                    base="a" * 40,
                    tip="b" * 40,
                    members=[],
                    claim_token="local-test",
                    expires_at=time.time() + 300,
                )
            )
            state = repair_task.TaskState(cfg.root / "tasks" / task.task_id, task)
            with patch.dict(os.environ, isolated, clear=True), patch.object(agents, "CLAUDE_CMD", binary):
                config = repair.prepare_claude_config(cfg.root, credentials=False)
                settings = config / "settings.json"
                existing = json.loads(settings.read_text())
                existing["env"] = {"CLAUDE_ENV_FILE": broken}
                settings.write_text(json.dumps(existing))
                first = repair_task.run_repair_agent(
                    cfg,
                    task,
                    checkout,
                    "Run the scripted stdin regression.",
                    root / "logs",
                    threading.Event(),
                    state,
                    {},
                    preselected=["api"],
                )
                self.assertEqual(first["status"], "ok", first)
                session = first["session"]
                self.assertTrue(session)
                self.assertEqual(len(api.calls), 4)
                # Resume must use the same checkout, irrespective of the last Bash cwd.
                api.commands = [
                    *commands,
                    None,
                    "pwd; cat <<'END'\nresume payload\nEND\ngrep -c exec; test $? -eq 1 && echo EOF_REACHED",
                ]
                resumed = repair_task.run_repair_agent(
                    cfg,
                    task,
                    checkout,
                    "Continue the scripted stdin regression.",
                    root / "resume-logs",
                    threading.Event(),
                    state,
                    {},
                    resume=session,
                    resume_provider="api",
                    preselected=["api"],
                )
                self.assertEqual(resumed["status"], "ok", resumed)
                self.assertEqual(resumed["session"], session)
                self.assertEqual(len(api.calls), 6)
                self.assertEqual(json.loads(settings.read_text()), existing)
            results = [
                "\n".join(
                    str(block.get("content", ""))
                    for block in call["last_message"].get("content", [])
                    if isinstance(block, dict) and block.get("type") == "tool_result"
                )
                for call in api.calls
            ]
            self.assertIn("heredoc payload", results[1])
            self.assertIn("cwd payload", results[2])
            self.assertIn("1\nexplicit heredoc\n1\n1\nexec\n0\nEOF_REACHED", results[3])
            self.assertIn("resume payload", results[5])
            self.assertEqual(results[3].splitlines()[0], str(checkout / "subdir"))
            self.assertEqual(results[5].splitlines()[0], str(checkout))
            for index in (1, 2, 3, 5):
                self.assertIn("EOF_REACHED", results[index])


if __name__ == "__main__":
    unittest.main(verbosity=2)

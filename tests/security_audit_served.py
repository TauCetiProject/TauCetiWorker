#!/usr/bin/env python3
"""Protocol 4 numbered reads, conservative served coverage and retained judgments."""

import concurrent.futures
import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import security_audit as historical

from tauceti_worker import audit_client_check as check
from tauceti_worker import audit_clients as clients
from tauceti_worker import audit_reader as ar
from tauceti_worker import security_audit as sa
from tauceti_worker.config import Die


def read_events(provider, pages, *, failed=False):
    events = []
    for page in pages:
        if provider == "codex":
            events.append(
                {
                    "type": "item.completed",
                    "item": {
                        "id": str(page),
                        "type": "mcp_tool_call",
                        "server": "audit",
                        "tool": "read_page",
                        "arguments": {"page": page},
                        "status": "failed" if failed else "completed",
                        "error": {"message": "client timeout"} if failed else None,
                        "result": None if failed else {"content": []},
                    },
                }
            )
        else:
            events.extend(
                [
                    {
                        "type": "assistant",
                        "message": {
                            "content": [
                                {
                                    "type": "tool_use",
                                    "id": str(page),
                                    "name": "mcp__audit__read_page",
                                    "input": {"page": page},
                                }
                            ]
                        },
                    },
                    {
                        "type": "user",
                        "message": {
                            "content": [
                                {"type": "tool_result", "tool_use_id": str(page), "is_error": failed, "content": []}
                            ]
                        },
                    },
                ]
            )
    return events


def read_all(reader, env):
    """Serve every page; a Claude client also stores each result in its session, as the real one does."""
    rows = []
    for page in range(len(reader.pages)):
        result = reader.request("tools/call", {"name": ar.TOOL, "arguments": {"page": page}})
        use = {"type": "tool_use", "id": f"read-{page}", "name": "mcp__audit__read_page", "input": {"page": page}}
        back = {"type": "tool_result", "tool_use_id": f"read-{page}", "content": result["content"]}
        rows += [{"type": "assistant", "message": {"content": [use]}}, {"type": "user", "message": {"content": [back]}}]
    if "CLAUDE_CONFIG_DIR" in env:
        session = Path(env["CLAUDE_CONFIG_DIR"]) / "projects" / "home" / "session.jsonl"
        session.parent.mkdir(parents=True, exist_ok=True)
        with session.open("a") as stream:
            stream.write("".join(sa.serialize(row) + "\n" for row in rows))


def native_read_rows(pages, batch):
    expected = ar.pages(batch)
    rows = []
    for page in pages:
        rows.extend(
            [
                {
                    "type": "response_item",
                    "payload": {
                        "type": "function_call",
                        "name": "read_page",
                        "arguments": sa.serialize({"page": page}),
                        "call_id": f"read-{page}",
                    },
                },
                {
                    "type": "response_item",
                    "payload": {
                        "type": "function_call_output",
                        "call_id": f"read-{page}",
                        "output": [
                            {"type": "input_text", "text": sa.serialize({"page": page, "sections": expected[page]})}
                        ],
                    },
                },
            ]
        )
    return rows


def append_native_reads(home, pages, thread, batch):
    directory = home / ".codex/sessions"
    directory.mkdir(parents=True, exist_ok=True)
    path = next(directory.glob("*.jsonl"), directory / "rollout.jsonl")
    if not path.exists():
        path.write_text(sa.serialize({"type": "session_meta", "payload": {"id": thread}}) + "\n")
    with path.open("a") as stream:
        stream.write("\n".join(map(sa.serialize, native_read_rows(pages, batch))) + "\n")


class NumberedReader(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.batch = check.test_batch()
        self.receipt = self.root / "served.json"
        self.reader = ar.Reader(self.batch, self.receipt)

    def read(self, page, reader=None, **kwargs):
        return (reader or self.reader).request("tools/call", {"name": ar.TOOL, "arguments": {"page": page}}, **kwargs)

    def test_exact_source_reconstruction_and_encoded_size_with_unicode_and_escaping(self):
        self.assertGreater(len(self.reader.pages), 3)
        collected = {}
        for page in range(len(self.reader.pages)):
            result = self.read(page)
            self.assertLessEqual(len(ar.encode(result).encode()), ar.RESPONSE_BYTES)
            text = result["content"][0]["text"]
            self.assertNotIn('"next_token"', text)
            for section in json.loads(text)["sections"]:
                key = section["path"], section["field"]
                self.assertEqual(section["start"], len(collected.get(key, "")))
                collected[key] = collected.get(key, "") + (section["text"] or "")
        for file in self.batch["commits"][0]["files"]:
            for field in ("before", "after", "diff"):
                self.assertEqual(collected[file["path"], field], file[field])
        self.assertEqual(self.reader.coverage()["served"], list(range(len(self.reader.pages))))

    def test_skip_rejected_reread_is_identical_and_restart_retains_progress(self):
        with self.assertRaisesRegex(ValueError, "cannot be skipped"):
            self.read(1)
        self.assertFalse(self.receipt.exists())
        first = self.read(0)
        original = self.receipt.read_bytes()
        again = ar.Reader(self.batch, self.receipt)
        self.assertEqual(self.read(0, again), first)
        self.assertEqual(original, self.receipt.read_bytes())
        self.read(1, again)
        self.assertEqual(again.coverage()["served"], [0, 1])

    def test_invalid_arguments_and_receipts_never_gain_coverage(self):
        for args in ({"page": True}, {"page": -1}, {"page": 0, "token": "anything"}, {}, {"page": "0"}):
            with self.subTest(args=args), self.assertRaises(ValueError):
                self.reader.request("tools/call", {"name": ar.TOOL, "arguments": args})
        self.read(0)
        valid = self.reader.coverage()
        for update in (
            {"input_digest": "other"},
            {"pages": True},
            {"served": [1]},
            {"served": [0, 0]},
            {"served": [False]},
            {"protocol_version": 3},
            {"extra": 1},
        ):
            self.receipt.write_text(ar.encode({**valid, **update}))
            with self.subTest(update=update), self.assertRaises(ValueError):
                self.read(1)

    def test_oversized_response_and_failed_output_flush_do_not_credit_page(self):
        with patch.object(ar, "RESPONSE_BYTES", 1), self.assertRaisesRegex(ValueError, "allowance"):
            self.read(0)
        self.assertFalse(self.receipt.exists())

        def broken_pipe(result):
            self.assertFalse(self.receipt.exists())
            raise BrokenPipeError("client exited")

        with self.assertRaises(BrokenPipeError):
            self.read(0, emit=broken_pipe)
        self.assertFalse(self.receipt.exists())
        delivered = []
        self.read(0, emit=delivered.append)
        self.assertEqual(len(delivered), 1)
        self.assertEqual(self.reader.coverage()["served"], [0])

    def test_concurrent_readers_cannot_drop_progress(self):
        self.read(0)

        def read_again(_):
            return self.read(0, ar.Reader(self.batch, self.receipt))

        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(read_again, range(24)))
        self.assertEqual(self.reader.coverage()["served"], [0])
        self.read(1)
        self.assertEqual(self.reader.coverage()["served"], [0, 1])

    def test_stdio_server_flushes_exact_response_then_records_coverage(self):
        source = self.root / "input.json"
        source.write_text(ar.encode(self.batch))
        request = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": ar.TOOL, "arguments": {"page": 0}},
        }
        expected = self.read(0)
        self.receipt.unlink()
        process = subprocess.run(
            [sys.executable, "-I", str(sa.reader_path()), str(source), str(self.receipt)],
            input=ar.encode(request) + "\n",
            text=True,
            capture_output=True,
            check=True,
        )
        self.assertEqual(json.loads(process.stdout)["result"], expected)
        self.assertEqual(self.reader.coverage()["served"], [0])


class ServedJudgments(unittest.TestCase):
    """Fresh protocol-5 inspections of a paged batch, through each reviewer's real launch path."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.work = Path(tmp.name) / "state" / sa.CURSOR
        self.work.mkdir(parents=True)
        self.thread = "11111111-2222-4333-8444-555555555555"
        text = "-- mathematics\n" * 4000
        self.batch = {
            "from_sha": "a" * 40,
            "main_sha": "c" * 40,
            "commits": [
                {
                    "sha": "b" * 40,
                    "parent": "a" * 40,
                    "files": [{"path": "TauCeti/A.lean", "before": "", "after": text, "diff": "+" + text}],
                }
            ],
        }
        self.batch["input_digest"] = hashlib.sha256(sa.serialize(self.batch).encode()).hexdigest()
        verified = patch.object(clients, "selected_protocol", return_value=5)
        verified.start()
        self.addCleanup(verified.stop)

    def test_both_inspectors_accept_only_verdict_and_findings_with_complete_served_coverage(self):
        (self.work / "auth.json").write_text('{"tokens":{"access_token":"fixture"}}')
        (self.work / ".credentials.json").write_text('{"claudeAiOauth":{"accessToken":"fixture"}}')
        value = {"verdict": "pass", "findings": []}
        for provider in ("codex", "claude"):
            attempt = self.work / ("attempt-" + provider)
            attempt.mkdir()

            def model(argv, *, provider=provider, **kwargs):
                if provider == "codex":
                    schema = json.loads((kwargs["cwd"] / "verdict-schema.json").read_text())
                    args = json.loads(next(v.split("=", 1)[1] for v in argv if v.startswith("mcp_servers.audit.args=")))
                else:
                    schema = json.loads(argv[argv.index("--json-schema") + 1])
                    config = json.loads(argv[argv.index("--mcp-config") + 1])
                    args = config["mcpServers"]["audit"]["args"]
                self.assertEqual(set(schema["properties"]), {"verdict", "findings"})
                self.assertEqual(set(schema["required"]), {"verdict", "findings"})
                self.assertEqual(len(args), 4)
                r = ar.Reader(json.loads(Path(args[2]).read_text()), Path(args[3]))
                read_all(r, kwargs["env"])
                if provider == "codex":
                    append_native_reads(kwargs["cwd"], range(len(r.pages)), self.thread, self.batch)
                    historical.append_native(kwargs["cwd"], value, self.thread)
                    events = [
                        {"type": "thread.started", "thread_id": self.thread},
                        {"type": "item.completed", "item": {"type": "agent_message", "text": sa.serialize(value)}},
                        {"type": "turn.completed"},
                    ]
                else:
                    events = [{"type": "result", "subtype": "success", "structured_output": value}]
                events = read_events(provider, range(len(r.pages))) + events
                return SimpleNamespace(returncode=0, stdout="\n".join(map(sa.serialize, events)), stderr="")

            with (
                patch.object(clients, "selected_protocol", return_value=5),
                patch.object(sa, "codex_dir", return_value=self.work),
                patch.object(sa, "claude_dir", return_value=self.work),
                patch.object(sa, "run_process", side_effect=model),
            ):
                self.assertEqual(sa.inspect(self.batch, provider, attempt), sa.bind_verdict(value, self.batch))
            self.assertEqual(json.loads((attempt / "execution.json").read_text())["protocol_version"], 5)

    def test_protocol_five_luna_continues_its_session_and_inert_helper_calls_do_not_block_a_pass(self):
        (self.work / "auth.json").write_text('{"tokens":{"access_token":"fixture"}}')
        attempt = self.work / "attempt-five"
        attempt.mkdir()
        launches = []

        def model(argv, **kwargs):
            launches.append(list(argv))
            catalog = json.loads((kwargs["cwd"] / "model-catalog.json").read_text())
            tools = catalog["models"][0]["model_messages"]["tools"]["mcp_resources"]
            self.assertEqual(
                {k: v["description"] for k, v in tools.items()}, dict.fromkeys(sa.INERT_TOOLS, sa.INERT_DESCRIPTION)
            )
            self.assertEqual(argv[argv.index("goals") - 1], "--disable")  # No goal tools in protocol 5.
            self.assertNotIn("--enable", argv)
            args = json.loads(next(v.split("=", 1)[1] for v in argv if v.startswith("mcp_servers.audit.args=")))
            r = ar.Reader(json.loads(Path(args[2]).read_text()), Path(args[3]))
            first = len(launches) == 1
            pages = [0] if first else range(1, len(r.pages))
            for page in pages:
                r.request("tools/call", {"name": ar.TOOL, "arguments": {"page": page}})
            append_native_reads(kwargs["cwd"], pages, self.thread, self.batch)
            sessions = kwargs["cwd"] / ".codex/sessions"
            with next(sessions.glob("*.jsonl")).open("a") as native:
                stray = {
                    "type": "response_item",
                    "payload": {"type": "function_call", "name": "read_mcp_resource", "arguments": "{}"},
                }
                native.write(sa.serialize(stray) + "\n")
            value = {"verdict": "unfinished" if first else "pass", "findings": []}
            historical.append_native(kwargs["cwd"], value, self.thread)
            events = [
                {"type": "thread.started", "thread_id": self.thread},
                *read_events("codex", pages),
                {"type": "item.completed", "item": {"type": "agent_message", "text": sa.serialize(value)}},
                {"type": "turn.completed"},
            ]
            return SimpleNamespace(returncode=0, stdout="\n".join(map(sa.serialize, events)), stderr="")

        with (
            patch.object(clients, "selected_protocol", return_value=5),
            patch.object(sa, "codex_dir", return_value=self.work),
            patch.object(sa, "run_process", side_effect=model),
        ):
            self.assertEqual(
                sa.inspect(self.batch, "codex", attempt),
                sa.bind_verdict({"verdict": "pass", "findings": []}, self.batch),
            )
        self.assertEqual(len(launches), 2)
        self.assertEqual(launches[1][-3:], ["resume", self.thread, "-"])
        self.assertEqual(json.loads((attempt / "execution.json").read_text())["protocol_version"], 5)

    def test_fresh_launch_carries_and_records_trusted_baseline_names(self):
        prompt = sa.judgment_prompt(self.batch, True)
        self.assertIn(sa.TRUSTED_ORIGIN_POLICY, prompt)
        self.assertIn("stacks", prompt.split("Trusted baseline names: ", 1)[1])
        self.assertIn("never makes safe", prompt)
        key_file = self.work / "anthropic-key"
        key_file.write_text("sk-ant-fixture\n")
        key_file.chmod(0o600)
        attempt = self.work / "attempt-names"
        attempt.mkdir()
        value = {"verdict": "pass", "findings": []}
        prompts = []

        def model(argv, **kwargs):
            prompts.append(kwargs["input"])
            config = json.loads(argv[argv.index("--mcp-config") + 1])
            args = config["mcpServers"]["audit"]["args"]
            r = ar.Reader(json.loads(Path(args[2]).read_text()), Path(args[3]))
            read_all(r, kwargs["env"])
            events = read_events("claude", range(len(r.pages))) + [
                {"type": "result", "subtype": "success", "structured_output": value}
            ]
            return SimpleNamespace(returncode=0, stdout="\n".join(map(sa.serialize, events)), stderr="")

        with (
            patch.object(clients, "selected_protocol", return_value=5),
            patch.dict(sa.os.environ, {sa.API_KEY_FILE_ENV: str(key_file)}),
            patch.object(sa, "run_process", side_effect=model),
        ):
            sa.inspect(self.batch, "claude", attempt)
        self.assertIn(sa.TRUSTED_ORIGIN_POLICY, prompts[0])
        execution = json.loads((attempt / "execution.json").read_text())
        self.assertEqual(execution["trusted_names_digest"], sa.trusted_names_digest())
        self.assertIn("claude-haiku-5-5", execution["argv"])

    def test_haiku_api_key_file_replaces_subscription_credentials_and_is_not_retained(self):
        key_file = self.work / "anthropic-key"
        key_file.write_text("sk-ant-fixture-secret\n")
        key_file.chmod(0o600)
        value = {"verdict": "pass", "findings": []}
        attempt = self.work / "attempt-api-key"
        attempt.mkdir()
        seen = {}

        def model(argv, **kwargs):
            seen.update(kwargs["env"])
            self.assertFalse((kwargs["cwd"] / ".claude/.credentials.json").exists())
            config = json.loads(argv[argv.index("--mcp-config") + 1])
            args = config["mcpServers"]["audit"]["args"]
            r = ar.Reader(json.loads(Path(args[2]).read_text()), Path(args[3]))
            read_all(r, kwargs["env"])
            events = read_events("claude", range(len(r.pages))) + [
                {"type": "result", "subtype": "success", "structured_output": value}
            ]
            return SimpleNamespace(returncode=0, stdout="\n".join(map(sa.serialize, events)), stderr="")

        with (
            patch.object(clients, "selected_protocol", return_value=5),
            patch.dict(sa.os.environ, {sa.API_KEY_FILE_ENV: str(key_file)}),
            patch.object(sa, "claude_dir", return_value=self.work / "no-subscription"),
            patch.object(sa, "run_process", side_effect=model),
        ):
            self.assertEqual(sa.inspect(self.batch, "claude", attempt), sa.bind_verdict(value, self.batch))
            key_file.chmod(0o644)
            with self.assertRaises(Die):
                sa.anthropic_api_key()
        self.assertEqual(seen["ANTHROPIC_API_KEY"], "sk-ant-fixture-secret")
        for path in attempt.rglob("*"):
            if path.is_file():
                self.assertNotIn(b"sk-ant-fixture-secret", path.read_bytes(), str(path))

    def test_pinned_claude_credential_selects_subscription_or_api_key(self):
        key_file = self.work / "anthropic-key"
        key_file.write_text("sk-ant-fixture-secret\n")
        key_file.chmod(0o600)
        (self.work / ".credentials.json").write_text('{"claudeAiOauth":{"accessToken":"subscription-fixture"}}')
        value = {"verdict": "pass", "findings": []}
        seen = []

        def model(argv, **kwargs):
            seen.append(
                (
                    kwargs["env"].get("ANTHROPIC_API_KEY"),
                    (kwargs["cwd"] / ".claude/.credentials.json").exists(),
                )
            )
            config = json.loads(argv[argv.index("--mcp-config") + 1])
            args = config["mcpServers"]["audit"]["args"]
            r = ar.Reader(json.loads(Path(args[2]).read_text()), Path(args[3]))
            read_all(r, kwargs["env"])
            events = read_events("claude", range(len(r.pages))) + [
                {"type": "result", "subtype": "success", "structured_output": value}
            ]
            return SimpleNamespace(returncode=0, stdout="\n".join(map(sa.serialize, events)), stderr="")

        with (
            patch.object(clients, "selected_protocol", return_value=5),
            patch.dict(sa.os.environ, {sa.API_KEY_FILE_ENV: str(key_file)}),
            patch.object(sa, "claude_dir", return_value=self.work),
            patch.object(sa, "run_process", side_effect=model),
        ):
            for kind in ("subscription", "api-key"):
                attempt = self.work / f"attempt-{kind}"
                attempt.mkdir()
                verdict = sa.inspect(self.batch, "claude", attempt, claude_credential=kind)
                self.assertEqual(verdict, sa.bind_verdict(value, self.batch))
        self.assertEqual(seen, [(None, True), ("sk-ant-fixture-secret", False)])
        with (
            patch.object(clients, "selected_protocol", return_value=5),
            patch.dict(sa.os.environ, {sa.API_KEY_FILE_ENV: ""}),
            patch.object(sa, "run_process", side_effect=model),
            self.assertRaises(Die),
        ):
            attempt = self.work / "attempt-missing-key"
            attempt.mkdir()
            sa.inspect(self.batch, "claude", attempt, claude_credential="api-key")
        self.assertEqual(len(seen), 2)


class ClientCompatibility(unittest.TestCase):
    def test_unrecognized_wrappers_do_not_claim_verified_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "wrapper"
            path.write_text("#!/bin/sh\nexec hidden-client --version\n")
            with (
                patch.object(clients, "client_path", return_value=path),
                self.assertRaisesRegex(ValueError, "native client"),
            ):
                clients.fingerprint("claude")

    def test_missing_stale_and_changed_clients_are_unverified(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            expected = {"client": "one"}
            with patch.object(clients, "fingerprint", return_value=expected):
                self.assertEqual(clients.selected_protocol("codex", state, verify=False), None)
                p = state / clients.RECORD
                p.write_text(sa.serialize({"codex": {"protocol_version": 5, "fingerprint": expected, "intact": True}}))
                self.assertEqual(clients.selected_protocol("codex", state, verify=False), 5)
                with patch.object(clients, "fingerprint", return_value={"client": "two"}):
                    self.assertEqual(clients.selected_protocol("codex", state, verify=False), None)
                self.assertEqual(clients.selected_protocol("claude", state, verify=False), None)
                p.chmod(0o666)
                self.assertEqual(clients.selected_protocol("codex", state, verify=False), None)

    def test_missing_and_changed_verification_refresh_automatically(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            current = {"client": "one"}

            def verify(provider, state):
                (state / clients.RECORD).write_text(
                    sa.serialize({provider: {"protocol_version": 5, "fingerprint": dict(current), "intact": True}})
                )

            with (
                patch.object(clients, "fingerprint", side_effect=lambda _: dict(current)),
                patch.object(check, "verify", side_effect=verify) as probe,
            ):
                self.assertEqual(clients.selected_protocol("codex", state), 5)
                self.assertEqual(clients.selected_protocol("codex", state), 5)
                self.assertEqual(probe.call_count, 1)
                current["client"] = "two"
                self.assertEqual(clients.selected_protocol("codex", state), 5)
                self.assertEqual(probe.call_count, 2)

    def test_concurrent_selection_runs_only_one_probe(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            expected = {"client": "one"}

            def verify(provider, state):
                from tauceti_worker.audit_alerts import save

                save(
                    state / clients.RECORD, {provider: {"protocol_version": 5, "fingerprint": expected, "intact": True}}
                )

            with (
                patch.object(clients, "fingerprint", return_value=expected),
                patch.object(check, "verify", side_effect=verify) as probe,
            ):
                with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
                    self.assertEqual(
                        list(pool.map(lambda _: clients.selected_protocol("codex", state), range(8))), [5] * 8
                    )
                probe.assert_called_once()

    def test_failed_probe_is_cached_retried_and_invalidated_by_client_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            with (
                patch.object(clients, "fingerprint", return_value={"client": "one"}) as fingerprint,
                patch.object(check, "verify", side_effect=RuntimeError("unexpected client format")) as probe,
                patch.object(clients.time, "time", return_value=1000) as now,
            ):
                self.assertEqual(clients.selected_protocol("codex", state), None)
                self.assertEqual(clients.selected_protocol("codex", state), None)
                self.assertEqual(probe.call_count, 1)
                now.return_value = 1301
                self.assertEqual(clients.selected_protocol("codex", state), None)
                self.assertEqual(probe.call_count, 2)
                fingerprint.return_value = {"client": "two"}
                self.assertEqual(clients.selected_protocol("codex", state), None)
                self.assertEqual(probe.call_count, 3)

    def test_capture_rejects_missing_or_truncated_middle_even_if_tail_survives(self):
        with tempfile.TemporaryDirectory() as tmp:
            capture = check.Capture(check.test_batch(), Path(tmp))
            text = capture.expected[0]
            for output in (text[: len(text) // 2], text[:100] + text[-100:], "not the source"):
                with self.subTest(output=output[:20]), self.assertRaises(ValueError):
                    capture.consume(
                        {
                            "input": [{"type": "function_call_output", "call_id": "audit_page_0", "output": output}],
                        },
                        "codex",
                    )
            self.assertEqual(capture.received, set())
            capture.consume(
                {
                    "input": [{"type": "function_call_output", "call_id": "audit_page_0", "output": text}],
                },
                "codex",
            )
            self.assertEqual(capture.received, {0})


if __name__ == "__main__":
    unittest.main()

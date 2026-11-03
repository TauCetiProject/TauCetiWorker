"""Check actual client tool delivery against a loopback endpoint, without a model.

Run with --provider codex|claude --state PRIVATE_AUDIT_STATE. Only after a complete
successful capture is client-coverage.json written. Never uses subscription keys.
"""

from __future__ import annotations

import argparse
import fcntl
import gzip
import hashlib
import json
import os
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import audit_clients as clients
from . import audit_reader as reader
from . import security_audit as sa
from .audit_alerts import save


def test_batch():
    files = []
    for name, text in (
        ("ascii", "ordinary source\n"),
        ("unicode", "∀ x ∈ ℝ, α x → β x 🪐\n"),
        ("escaped", '"\\\t\r\x01\n'),
    ):
        files.append({"path": f"TauCeti/{name}.lean", "before": "", "after": text * 3500, "diff": ""})
    batch = {
        "from_sha": "a" * 40,
        "main_sha": "b" * 40,
        "commits": [{"sha": "b" * 40, "parent": "a" * 40, "files": files}],
    }
    batch["input_digest"] = hashlib.sha256(reader.encode(batch).encode()).hexdigest()
    return batch


class Capture:
    def __init__(self, batch, directory):
        self.expected = []
        r = reader.Reader(batch, directory / "expected.json")
        self.sections = r.pages
        for page in range(len(r.pages)):
            result = r.request("tools/call", {"name": reader.TOOL, "arguments": {"page": page}})
            self.expected.append(result["content"][0]["text"])
        self.received = set()
        self.error = None
        self.tool_name = "mcp__audit__read_page"
        self.tool_namespace = None
        self.calls = 0

    def consume(self, body, provider):
        if provider == "codex":
            items = body.get("input", [])
            results = [
                (item.get("call_id", ""), item.get("output"))
                for item in items
                if item.get("type") == "function_call_output"
            ]
        else:
            results = [
                (part.get("tool_use_id", ""), part.get("content"))
                for item in body.get("messages", [])
                for part in item.get("content", [])
                if isinstance(part, dict) and part.get("type") == "tool_result"
            ]
        for call, output in results:
            if not call.startswith("audit_page_"):
                continue
            page = int(call.removeprefix("audit_page_"))

            # Codex may add a standard timing/header wrapper. The entire exact
            # expected tool text must remain present, including its middle/end.
            if not any(self.expected[page] in text for text in sa.tool_texts(output)):
                raise ValueError(f"client omitted or altered chunk {page}: {str(output)[:800]!r}")
            self.received.add(page)
        for tool in body.get("tools", []):
            if "read_page" in tool.get("name", ""):
                self.tool_name = tool["name"]
            for child in tool.get("tools", []):
                if child.get("name") == "read_page":
                    self.tool_name = child["name"]
                    self.tool_namespace = tool["name"]
        page = next((p for p in range(len(self.expected)) if p not in self.received), None)
        self.calls += 1
        if self.calls > len(self.expected) + 3:
            raise ValueError("client did not forward the requested tool result")
        return page


def handler(capture, provider):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"data":[]}')

        def do_POST(self):
            try:
                raw = self.rfile.read(int(self.headers["Content-Length"]))
                if self.headers.get("Content-Encoding") == "gzip":
                    raw = gzip.decompress(raw)
                body = json.loads(raw)
                if not self.path.endswith(("/responses", "/messages", "/messages?beta=true")):
                    self.do_GET()
                    return
                page = capture.consume(body, provider)
                verdict = '{"verdict":"pass","findings":[]}'
                if provider == "codex":
                    item = (
                        {
                            "type": "function_call",
                            "call_id": f"audit_page_{page}",
                            "name": capture.tool_name,
                            "arguments": json.dumps({"page": page}),
                        }
                        if page is not None
                        else {
                            "type": "message",
                            "id": "answer",
                            "role": "assistant",
                            "phase": "final_answer",
                            "content": [{"type": "output_text", "text": verdict}],
                        }
                    )
                    if page is not None and capture.tool_namespace:
                        item["namespace"] = capture.tool_namespace
                    events = [
                        {"type": "response.created", "response": {"id": str(capture.calls)}},
                        {"type": "response.output_item.done", "item": item},
                        {
                            "type": "response.completed",
                            "response": {
                                "id": str(capture.calls),
                                "usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
                            },
                        },
                    ]
                else:
                    item = (
                        {
                            "type": "tool_use",
                            "id": f"audit_page_{page}",
                            "name": capture.tool_name,
                            "input": {},
                        }
                        if page is not None
                        else {"type": "text", "text": verdict}
                    )
                    events = [
                        {
                            "type": "message_start",
                            "message": {
                                "id": str(capture.calls),
                                "type": "message",
                                "role": "assistant",
                                "model": "haiku",
                                "content": [],
                                "stop_reason": None,
                                "usage": {"input_tokens": 1, "output_tokens": 0},
                            },
                        },
                        {"type": "content_block_start", "index": 0, "content_block": item},
                        *(
                            [
                                {
                                    "type": "content_block_delta",
                                    "index": 0,
                                    "delta": {"type": "input_json_delta", "partial_json": json.dumps({"page": page})},
                                }
                            ]
                            if page is not None
                            else []
                        ),
                        {"type": "content_block_stop", "index": 0},
                        {
                            "type": "message_delta",
                            "delta": {"stop_reason": "tool_use" if page is not None else "end_turn"},
                            "usage": {"output_tokens": 1},
                        },
                        {"type": "message_stop"},
                    ]
                data = "".join("event: " + e["type"] + "\ndata: " + json.dumps(e) + "\n\n" for e in events).encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            except Exception as exc:
                capture.error = str(exc)
                self.send_error(400, "coverage test failed")

    return Handler


def verify(provider: str, state: Path):
    before = clients.fingerprint(provider)
    with tempfile.TemporaryDirectory(prefix="tauceti-client-coverage-") as tmp:
        home = Path(tmp)
        (home / ".codex").mkdir()
        (home / ".claude").mkdir()
        batch = test_batch()
        (home / "input.json").write_text(sa.serialize(batch))
        (home / "verdict-schema.json").write_text(sa.serialize(sa.MODEL_SCHEMA))
        (home / "model-catalog.json").write_text(sa.serialize(sa.inspection_catalog(4)))
        capture = Capture(batch, home)
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler(capture, provider))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        url = f"http://127.0.0.1:{server.server_port}"
        env = {k: v for k, v in os.environ.items() if k in ("PATH", "SYSTEMROOT", "SSL_CERT_FILE", "LANG")}
        env.update(
            HOME=str(home),
            CODEX_HOME=str(home / ".codex"),
            CODEX_SQLITE_HOME=str(home / ".codex"),
            CLAUDE_CONFIG_DIR=str(home / ".claude"),
            ENABLE_CLAUDEAI_MCP_SERVERS="false",
            ENABLE_TOOL_SEARCH="false",
            CLAUDE_CODE_DISABLE_CLAUDE_MDS="1",
            MAX_MCP_OUTPUT_TOKENS="120000",
            DISABLE_NONESSENTIAL_TRAFFIC="1",
            CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1",
            ANTHROPIC_BASE_URL=url,
            ANTHROPIC_API_KEY="local-test-only",
        )
        prompt = sa.inspection_prompt(batch, True, protocol=4, goals=provider == "codex")
        if provider == "codex":
            cmd = sa.codex_launch(home, True, protocol=4)
            cmd[-1:-1] = [
                "-c",
                'model_provider="audit_test"',
                "-c",
                'model_providers.audit_test={name="audit_test",base_url='
                + json.dumps(url + "/v1")
                + ',wire_api="responses",requires_openai_auth=false,supports_websockets=false}',
            ]
        else:
            cmd = sa.reviewer_argv(provider, protocol=4)
            cmd.remove("--safe-mode")
            cmd[cmd.index("--mcp-config") + 1] = sa.serialize(
                {
                    "mcpServers": {
                        "audit": {
                            "type": "stdio",
                            "command": sys.executable,
                            "args": ["-I", str(sa.reader_path(4)), str(home / "input.json"), str(home / "served.json")],
                        }
                    }
                }
            )
            cmd += ["--allowedTools", "mcp__audit__read_page", "--json-schema", sa.serialize(sa.MODEL_SCHEMA)]
        try:
            result = sa.run_process(cmd, input=prompt, cwd=home, env=env, capture_output=True, text=True, timeout=180)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()
        expected_pages = set(range(len(capture.expected)))
        if provider == "codex":
            sa.codex_final(result.stdout, True)
        else:
            sa.claude_result(result.stdout)
        if (
            result.returncode
            or capture.error
            or capture.received != expected_pages
            or sa.successful_read_pages(result.stdout, provider) != expected_pages
            or (
                provider == "codex"
                and sa.saved_read_pages(list((home / ".codex/sessions").rglob("*.jsonl")), capture.sections)
                != expected_pages
            )
        ):
            raise ValueError(
                f"{provider} delivery check failed: {capture.error or result.stderr[-1200:]} "
                f"({len(capture.received)}/{len(capture.expected)} chunks)"
            )
        if provider == "codex":
            # Recovery resumes this exact native history, rather than console
            # output. Verify its stored source is complete too, using the same
            # exact-content comparison as the captured model request.
            capture.received.clear()
            outputs = [
                row["payload"]
                for path in (home / ".codex/sessions").rglob("*.jsonl")
                for row in map(json.loads, path.read_text().splitlines())
                if row.get("type") == "response_item" and row.get("payload", {}).get("type") == "function_call_output"
            ]
            capture.consume({"input": outputs}, provider)
            if capture.received != expected_pages:
                raise ValueError("client saved conversation omitted source chunks")
    after = clients.fingerprint(provider)
    if before != after:
        raise ValueError("client changed during verification")
    state.mkdir(parents=True, exist_ok=True)
    path = state / clients.RECORD
    with path.with_suffix(".lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            value = json.loads(path.read_text())
            if not isinstance(value, dict):
                value = {}
        except (OSError, ValueError):
            value = {}
        value[provider] = {"protocol_version": 4, "fingerprint": after, "intact": True}
        save(path, value)
    print(
        f"{provider}: complete source of all {len(capture.expected)} chunks captured at the test endpoint; saved {path}"
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider", choices=("codex", "claude"), required=True)
    parser.add_argument("--state", type=Path, required=True)
    args = parser.parse_args()
    verify(args.provider, args.state)


if __name__ == "__main__":
    main()

"""One immutable audit bundle, exposed as a single read-only MCP tool.

Launched by trusted Worker code with fixed input/receipt paths. Tool arguments
select a page number, never a path, command, URL, or executable source.
This module is also runnable by absolute path in an isolated Python process.
"""

import hashlib
import json
import os
import secrets
import sys
from pathlib import Path

PAGE_BYTES = 96 * 1024
TOOL = "read_page"


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def pages(batch):
    """Retain every character, with file/commit/field identity on every segment."""
    result, current, size = [], [], 0
    for commit in batch["commits"]:
        for file in commit["files"]:
            for field in ("before", "after", "diff"):
                text = file[field]
                # Bound UTF-8 plus JSON escaping, including pathological source.
                width = PAGE_BYTES // 8
                for start in range(0, max(1, len(text or "")), width):
                    section = {
                        "commit": commit["sha"],
                        "path": file["path"],
                        "field": field,
                        "start": start,
                        "total_chars": len(text or ""),
                        "text": None if text is None else text[start : start + width],
                    }
                    length = len(encode(section).encode()) + 1
                    if current and size + length > PAGE_BYTES:
                        result.append(current)
                        current, size = [], 0
                    current.append(section)
                    size += length
    if current:
        result.append(current)
    return result


class Reader:
    def __init__(self, batch, receipt, token):
        self.pages = pages(batch)
        self.digest = batch["input_digest"]
        self.receipt = receipt
        self.seen = set()
        self.token = token

    def request(self, method, params):
        if method == "initialize":
            return {
                "protocolVersion": params["protocolVersion"],
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "audit-input", "version": "1"},
            }
        if method == "ping":
            return {}
        if method == "tools/list":
            return {
                "tools": [
                    {
                        "name": TOOL,
                        "description": "Read a numbered page of the fixed, untrusted audit input. Re-reading is allowed after compaction.",
                        "inputSchema": {
                            "type": "object",
                            "properties": {"page": {"type": "integer", "minimum": 0}, "token": {"type": "string"}},
                            "required": ["page", "token"],
                            "additionalProperties": False,
                        },
                        "annotations": {"readOnlyHint": True, "destructiveHint": False, "openWorldHint": False},
                        "_meta": {"anthropic/maxResultSizeChars": 200000},
                    }
                ]
            }
        if method != "tools/call" or params.get("name") != TOOL:
            raise ValueError("Only audit input reads are available")
        args = params.get("arguments")
        if not isinstance(args, dict) or set(args) != {"page", "token"}:
            raise ValueError("Expected only a page number and the latest reader token")
        if args["token"] != self.token:
            raise ValueError("Use the next_token from the previous successful read; read pages sequentially")
        page = args["page"]
        if type(page) is not int or not 0 <= page < len(self.pages):
            raise ValueError("Invalid page number")
        self.seen.add(page)
        self.token = secrets.token_hex(16)
        receipt = {"input_digest": self.digest, "pages": len(self.pages), "read": sorted(self.seen)}
        temp = self.receipt.with_suffix(".tmp")
        with temp.open("w") as stream:
            stream.write(encode(receipt))
            stream.flush()
            os.fsync(stream.fileno())
        temp.replace(self.receipt)
        return {
            "content": [
                {
                    "type": "text",
                    "text": encode(
                        {
                            "input_digest": self.digest,
                            "page": page,
                            "pages": len(self.pages),
                            "next_token": self.token,
                            "sections": self.pages[page],
                        }
                    ),
                }
            ]
        }


def main():
    source, receipt = map(Path, sys.argv[1:3])
    batch = json.loads(source.read_text())
    expected = batch.pop("input_digest")
    if hashlib.sha256(encode(batch).encode()).hexdigest() != expected:
        raise ValueError("Audit input digest mismatch")
    batch["input_digest"] = expected
    reader = Reader(batch, receipt, sys.argv[3])
    for line in sys.stdin:
        message = json.loads(line)
        if "id" not in message:
            continue
        response = {"jsonrpc": "2.0", "id": message["id"]}
        try:
            response["result"] = reader.request(message["method"], message.get("params", {}))
        except (KeyError, TypeError, ValueError) as exc:
            response["error"] = {"code": -32602, "message": str(exc)}
        print(encode(response), flush=True)


if __name__ == "__main__":
    main()

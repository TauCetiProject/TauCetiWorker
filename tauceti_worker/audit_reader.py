"""One immutable audit bundle, exposed as a single read-only MCP tool.

Launched by trusted Worker code with fixed input/receipt paths. Tool arguments
select a page number, never a path, command, URL, or executable source.
This module is also runnable by absolute path in an isolated Python process.
"""

import fcntl
import hashlib
import json
import os
import sys
from pathlib import Path

# Complete encoded responses stay below 64 KiB, leaving at least 56 KiB of
# headroom under the configured 120k client allowance even with byte tokenization.
PAGE_BYTES = 48 * 1024
RESPONSE_BYTES = 64 * 1024
PROTOCOL_VERSION = 4
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
                width = PAGE_BYTES // 16
                start = 0
                while True:
                    end = min(start + width, len(text or ""))
                    if text and end < len(text):
                        newline = text.rfind("\n", start, end)
                        if newline >= start:
                            end = newline + 1
                    section = {
                        "commit": commit["sha"],
                        "path": file["path"],
                        "field": field,
                        "start": start,
                        "total_chars": len(text or ""),
                        "text": None if text is None else text[start:end],
                        "prefix": (text or "")[max(0, start - 256) : start],
                    }
                    length = len(encode(encode(section)).encode()) + 1
                    if length > PAGE_BYTES:
                        raise ValueError("Audit path/section metadata exceeds the reader page limit")
                    if current and size + length > PAGE_BYTES:
                        result.append(current)
                        current, size = [], 0
                    current.append(section)
                    size += length
                    if end == len(text or ""):
                        break
                    start = end
    if current:
        result.append(current)
    return result


class Reader:
    def __init__(self, batch, receipt):
        self.pages = pages(batch)
        self.digest = batch["input_digest"]
        self.receipt = receipt

    def coverage(self):
        if not self.receipt.exists():
            return {
                "protocol_version": PROTOCOL_VERSION,
                "input_digest": self.digest,
                "pages": len(self.pages),
                "served": [],
            }
        value = json.loads(self.receipt.read_text(encoding="utf-8"))
        if (
            not isinstance(value, dict)
            or set(value) != {"protocol_version", "input_digest", "pages", "served"}
            or value["protocol_version"] != PROTOCOL_VERSION
            or type(value["pages"]) is not int
            or value["pages"] != len(self.pages)
            or value["input_digest"] != self.digest
            or not isinstance(value["served"], list)
            or any(type(i) is not int for i in value["served"])
            or value["served"] != list(range(len(value["served"])))
            or len(value["served"]) > len(self.pages)
        ):
            raise ValueError("Invalid retained served coverage receipt")
        return value

    def request(self, method, params, *, emit=None):
        if not isinstance(params, dict):
            raise ValueError("Expected object parameters")
        if method == "initialize":
            return {
                "protocolVersion": params["protocolVersion"],
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "audit-input", "version": "4"},
            }
        if method == "ping":
            return {}
        if method == "tools/list":
            return {
                "tools": [
                    {
                        "name": TOOL,
                        "description": "Read the next unserved numbered chunk of the fixed, untrusted audit input, or reread an earlier chunk. Calls are serialized.",
                        "inputSchema": {
                            "type": "object",
                            "properties": {"page": {"type": "integer", "minimum": 0}},
                            "required": ["page"],
                            "additionalProperties": False,
                        },
                        "annotations": {"readOnlyHint": True, "destructiveHint": False, "openWorldHint": False},
                        "_meta": {"anthropic/maxResultSizeChars": 120000},
                    }
                ]
            }
        if method != "tools/call" or params.get("name") != TOOL:
            raise ValueError("Only audit input reads are available")
        args = params.get("arguments")
        if not isinstance(args, dict) or set(args) != {"page"} or type(args["page"]) is not int:
            raise ValueError("Expected only an integer page number")
        page = args["page"]
        if not 0 <= page < len(self.pages):
            raise ValueError("Invalid page number")
        # Reload inside a process-shared lock: concurrent calls and separate
        # reader processes cannot overwrite one another's progress.
        with self.receipt.with_suffix(".lock").open("a+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            proof = self.coverage()
            count = len(proof["served"])
            if page > count:
                raise ValueError(f"Read the next unserved page {count}; later pages cannot be skipped to")
            new_count = max(count, page + 1)
            result = {
                "content": [
                    {
                        "type": "text",
                        "text": encode(
                            {
                                "page": page,
                                "pages": len(self.pages),
                                "sections": self.pages[page],
                                "next_unserved_page": new_count if new_count < len(self.pages) else None,
                                "remaining_pages": len(self.pages) - new_count,
                            }
                        ),
                    }
                ]
            }
            # Check the COMPLETE MCP result, including JSON escaping and metadata,
            # before crediting it. A receipt records service, not model receipt.
            if len(encode(result).encode("utf-8")) > RESPONSE_BYTES:
                raise ValueError("Audit response exceeds the client output allowance")
            # The stdio server flushes the full response before committing served
            # coverage. A broken pipe gains no credit; a crash after flush and
            # before commit safely requires rereading. Keep the lock throughout.
            if emit is not None:
                emit(result)
            if new_count != count:
                proof["served"] = list(range(new_count))
                temp = self.receipt.with_suffix(".tmp")
                with temp.open("w", encoding="utf-8") as stream:
                    stream.write(encode(proof))
                    stream.flush()
                    os.fsync(stream.fileno())
                temp.replace(self.receipt)
            return result


def main():
    if len(sys.argv) != 3:
        raise ValueError("Expected fixed input and receipt paths")
    source, receipt = map(Path, sys.argv[1:3])
    batch = json.loads(source.read_text())
    expected = batch.pop("input_digest")
    if hashlib.sha256(encode(batch).encode()).hexdigest() != expected:
        raise ValueError("Audit input digest mismatch")
    batch["input_digest"] = expected
    reader = Reader(batch, receipt)
    for line in sys.stdin:
        message = json.loads(line)
        if "id" not in message:
            continue
        response = {"jsonrpc": "2.0", "id": message["id"]}
        try:
            if message["method"] == "tools/call":
                reader.request(
                    message["method"],
                    message.get("params", {}),
                    emit=lambda result, response=response: print(encode({**response, "result": result}), flush=True),
                )
                continue
            response["result"] = reader.request(message["method"], message.get("params", {}))
        except (KeyError, TypeError, ValueError) as exc:
            response["error"] = {"code": -32602, "message": str(exc)}
        print(encode(response), flush=True)


if __name__ == "__main__":
    main()

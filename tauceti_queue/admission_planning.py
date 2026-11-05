"""Admission knowledge is navigation for repairs, never an admission or CI verdict.

Bors's selection replay already happens in the queue's insertion probe. Preserve its
conflict paths and recover the other useful evidence: shared paths and import components.
Compiled declaration records describe accepted inputs, so edited/empty members do not
trigger another repair from names their accepted input used to add.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from collections import defaultdict
from pathlib import Path

from .declaration_names import accepted

MAX_FILE_BYTES = 256 * 1024
MAX_BLOB_BYTES = 8 * 1024 * 1024
MAX_RECORDS = 4096
MAX_NAMES = 262144


def imports(text: str) -> list[str]:
    """A conservative header scan; import edges remain explicitly approximate.

    Strip nested Lean comments before reading header commands. Stop at source code;
    a later string or comment containing `import` cannot become a dependency.
    """
    from .repair_context import _strip_comments

    out = []
    for line in _strip_comments(text.splitlines()):
        line = line.strip()
        if not line or line in ("module", "prelude"):
            continue
        match = re.fullmatch(r"(?:(?:public|private|meta)\s+)*import\s+([\w.']+)", line)
        if match:
            out.append(match[1])
        else:
            break
    return out


def fingerprint(records: list[dict]) -> str:
    return hashlib.sha256(json.dumps(records, sort_keys=True).encode()).hexdigest()


class Planner:
    """Cache immutable Git evidence; no checkout, source execution or network access."""

    def __init__(self, clone: Path):
        self.clone = clone
        self.changes: dict[str, list[tuple[str, str | None]]] = {}
        self.headers: dict[str, list[str]] = {}

    def git(self, *args: str, data: bytes | None = None) -> bytes:
        return subprocess.run(
            ["git", "-C", str(self.clone), *args], input=data, capture_output=True, check=True, timeout=15
        ).stdout

    def load(self, commits: list[str]) -> None:
        missing = sorted(set(commits) - self.changes.keys())
        if missing:
            raw = (
                self.git(
                    "diff-tree",
                    "--stdin",
                    "--root",
                    "--raw",
                    "--no-abbrev",
                    "--no-renames",
                    "-r",
                    "-z",
                    "--always",
                    data=("\n".join(missing) + "\n").encode(),
                )
                .decode()
                .split("\0")
            )
            pos = 0
            for sha in missing:
                if raw[pos] != sha:
                    raise ValueError("unexpected diff-tree identity")
                pos += 1
                changes = []
                while pos < len(raw) and raw[pos].startswith(":"):
                    meta, path = raw[pos].split(), raw[pos + 1]
                    # Symlinks and deleted blobs are paths, never source to inspect.
                    blob = meta[3] if meta[1] == "100644" else None
                    changes.append((path, blob))
                    pos += 2
                self.changes[sha] = changes
        blobs = sorted({b for sha in commits for p, b in self.changes[sha] if p.endswith(".lean") and b})
        blobs = [b for b in blobs if b not in self.headers]
        if not blobs:
            return
        sizes = (
            self.git(
                "cat-file",
                "--batch-check=%(objectname) %(objecttype) %(objectsize)",
                data=("\n".join(blobs) + "\n").encode(),
            )
            .decode()
            .splitlines()
        )
        selected, total = [], 0
        for row in sizes:
            sha, kind, size = row.split()
            total += int(size)
            if kind != "blob" or int(size) > MAX_FILE_BYTES or total > MAX_BLOB_BYTES:
                raise ValueError("import context byte budget exceeded")
            selected.append(sha)
        raw = self.git("cat-file", "--batch", data=("\n".join(selected) + "\n").encode())
        pos = 0
        for sha in selected:
            end = raw.index(b"\n", pos)
            oid, kind, size = raw[pos:end].decode().split()
            if oid != sha or kind != "blob":
                raise ValueError("unexpected blob identity")
            end_data = end + 1 + int(size)
            self.headers[sha] = imports(raw[end + 1 : end_data].decode())
            pos = end_data + 1

    def context(self, members: list[dict], inputs: dict[int, dict], *, tip: str) -> dict:
        records, errors = [], []
        active = [m for m in members if not m.get("withdrawn")]
        try:
            self.load([m["commit_sha"] for m in active])
        except (ValueError, UnicodeError, subprocess.SubprocessError) as error:
            errors.append(type(error).__name__ + ": " + str(error)[:200])
        owners: dict[str, set[int]] = defaultdict(set)
        added: dict[str, list[dict]] = defaultdict(list)
        total_names = 0
        names_limited = False
        for m in active:
            pr = m["number"]
            for path, _ in self.changes.get(m["commit_sha"], []):
                owners[path].add(pr)
            input_record = inputs.get(m["input_id"], {})
            evidence = input_record.get("evidence") or {}
            if isinstance(evidence, str):
                evidence = json.loads(evidence)
            records.extend(evidence.get("merge_selection", []))
            record = input_record.get("declaration_names")
            if isinstance(record, str):
                record = json.loads(record)
            record = accepted(record, m["accepted_head"])
            if record is None or m["provenance"] != "clean" or not self.changes.get(m["commit_sha"]):
                continue
            for module, names in record.get("added", {}).items():
                for name in names:
                    total_names += 1
                    if total_names > MAX_NAMES:
                        names_limited = True
                        break
                    added[name].append({"pr": pr, "module": module, "accepted_head": m["accepted_head"]})
        if names_limited:
            errors.append("compiled-name context budget exceeded")
        # Bound the in-memory cache to active workloads across a long-lived service.
        if len(self.changes) > 32768:
            live = {m["commit_sha"] for m in active}
            self.changes = {s: c for s, c in self.changes.items() if s in live}
            blobs = {b for changes in self.changes.values() for _, b in changes}
            self.headers = {b: h for b, h in self.headers.items() if b in blobs}
        for name, sources in sorted(added.items()):
            if len({s["pr"] for s in sources}) > 1:
                records.append(
                    {
                        "kind": "declaration_collision",
                        "name": name,
                        "sources": sources,
                        "approximate": False,
                        "prediction": True,
                    }
                )
        for path, prs in sorted(owners.items()):
            if len(prs) > 1:
                records.append({"kind": "path_overlap", "path": path, "members": sorted(prs)})
        edges = set()
        for m in active:
            for path, blob in self.changes.get(m["commit_sha"], []):
                for module in self.headers.get(blob, []):
                    provider = module.replace(".", "/") + ".lean"
                    for owner in owners.get(provider, set()) - {m["number"]}:
                        edges.add((m["number"], owner, path, provider))
        # Connected components compress indirect import interactions, including shared paths.
        graph: dict[int, set[int]] = defaultdict(set)
        for a, b, _, _ in edges:
            graph[a].add(b)
            graph[b].add(a)
        for prs in owners.values():
            for pr in prs:
                graph[pr].update(prs - {pr})
        seen = set()
        for start in sorted(graph):
            if start in seen:
                continue
            todo, component = [start], set()
            while todo:
                pr = todo.pop()
                if pr in component:
                    continue
                component.add(pr)
                todo.extend(graph[pr] - component)
            seen.update(component)
            if len(component) > 1:
                records.append(
                    {
                        "kind": "import_component",
                        "members": sorted(component),
                        "approximate": True,
                        "edges": [
                            {"consumer": a, "provider": b, "path": p, "provider_path": q}
                            for a, b, p, q in sorted(edges)
                            if a in component
                        ],
                    }
                )
        truncated = len(records) > MAX_RECORDS
        return {
            "kind": "admission_predictions",
            "tip": tip,
            "records": records[:MAX_RECORDS],
            "complete": not errors and not truncated,
            "errors": errors,
            "truncated": truncated,
            "message": "Admission-time predictions and overlaps; the worker's scripted build and audits decide findings.",
        }


def prompt_lines(context: dict | None) -> list[str]:
    if not context:
        return []
    lines = ["Admission-time knowledge (predictions and navigation; scripted audits remain authoritative):", ""]
    for record in context.get("records", [])[:40]:
        kind = record["kind"]
        if kind == "declaration_collision":
            owners = ", ".join(f"#{s['pr']} in {s['module']}" for s in record["sources"])
            lines.append(f"- Compiled accepted inputs both add `{record['name']}`: {owners}.")
        elif kind == "path_overlap":
            lines.append(f"- Shared path `{record['path']}`: " + ", ".join(f"#{p}" for p in record["members"]) + ".")
        elif kind == "selection_conflict":
            lines.append(
                f"- Insertion of #{record['pr']} conflicted with batch {record['batch_id']} at "
                + ", ".join(record["paths"])
                + "; interacting members: "
                + ", ".join(f"#{p}" for p in record["members"])
                + "."
            )
        else:
            lines.append(
                "- Connected import/shared-path component: " + ", ".join(f"#{p}" for p in record["members"]) + "."
            )
    if not context.get("complete", True):
        lines.append("This context is incomplete; missing evidence is unknown.")
    lines += ["The full retained admission context is in `.tauceti-repair/admission.json`.", ""]
    return lines

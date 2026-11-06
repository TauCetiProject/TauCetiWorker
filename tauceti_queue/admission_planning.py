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
MAX_EDGES = 2048
MAX_CONTEXT_BYTES = 512 * 1024
PROJECT_MODULE_PREFIX = "TauCeti."
MAX_GAP_TREES = 64  # memoised whole-tree import scans, keyed by tree id
MAX_GAP_CANDIDATES = 64  # importers of an absent source one scan confirms by header before giving up
# One line of a Lean import header naming a project module (POSIX ERE for `git grep`, and the
# Python form that extracts the module from a matched line).
IMPORT_LINE_ERE = (
    r"^[[:space:]]*((public|private|meta)[[:space:]]+)*import[[:space:]]+TauCeti(\.[A-Za-z0-9_']+)+[[:space:]]*$"
)
IMPORT_LINE = re.compile(r"^\s*(?:(?:public|private|meta)\s+)*import\s+(TauCeti(?:\.[A-Za-z0-9_']+)+)\s*$")


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
        self.omitted: dict[str, str] = {}
        self.pending = False
        self.gaps: dict[str, tuple[frozenset[tuple[str, str]] | None, str | None]] = {}

    def git(self, *args: str, data: bytes | None = None) -> bytes:
        return subprocess.run(
            ["git", "-C", str(self.clone), *args], input=data, capture_output=True, check=True, timeout=15
        ).stdout

    def load(self, commits: list[str]) -> None:
        self.pending = False
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
        self.read_headers(blobs)

    def read_headers(self, blobs: list[str]) -> None:
        """Parse the import headers of `blobs` not yet cached, within one byte-budgeted pass.

        Blobs over `MAX_FILE_BYTES` or non-text are recorded in `omitted`; blobs beyond the pass's
        `MAX_BLOB_BYTES` budget are left for another pass and `pending` is set.
        """
        blobs = [b for b in blobs if b not in self.headers and b not in self.omitted]
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
            if kind != "blob" or int(size) > MAX_FILE_BYTES:
                self.omitted[sha] = "import context byte budget exceeded"
                continue
            if total + int(size) > MAX_BLOB_BYTES:
                self.pending = True
                continue
            total += int(size)
            selected.append(sha)
        raw = self.git("cat-file", "--batch", data=("\n".join(selected) + "\n").encode())
        pos = 0
        for sha in selected:
            end = raw.index(b"\n", pos)
            oid, kind, size = raw[pos:end].decode().split()
            if oid != sha or kind != "blob":
                raise ValueError("unexpected blob identity")
            end_data = end + 1 + int(size)
            try:
                self.headers[sha] = imports(raw[end + 1 : end_data].decode())
            except UnicodeError:
                self.omitted[sha] = "non-text import context"
            pos = end_data + 1

    def context(self, members: list[dict], inputs: dict[int, dict], *, tip: str) -> dict:
        records, errors = [], []
        retryable = False
        active = [m for m in members if not m.get("withdrawn")]
        try:
            self.load([m["commit_sha"] for m in active])
        except (ValueError, UnicodeError, subprocess.SubprocessError) as error:
            errors.append(type(error).__name__ + ": " + str(error)[:200])
            retryable = True
        errors.extend(
            sorted(
                {self.omitted[b] for m in active for _, b in self.changes.get(m["commit_sha"], []) if b in self.omitted}
            )
        )
        if self.pending:
            errors.append("import context awaiting another byte-budgeted pass")
            retryable = True
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
            self.omitted = {b: why for b, why in self.omitted.items() if b in blobs}
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
                        if len(edges) < MAX_EDGES:
                            edges.add((m["number"], owner, path, provider))
                        elif "import edge budget exceeded" not in errors:
                            errors.append("import edge budget exceeded")
        # Connected components compress indirect import interactions, including shared paths.
        graph: dict[int, set[int]] = defaultdict(set)
        for a, b, _, _ in edges:
            graph[a].add(b)
            graph[b].add(a)
        for prs in owners.values():
            # A star connects the same component without a quadratic clique.
            anchor = min(prs)
            for pr in prs - {anchor}:
                graph[pr].add(anchor)
                graph[anchor].add(pr)
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
        records.sort(
            key=lambda r: {
                "declaration_collision": 0,
                "selection_conflict": 1,
                "path_overlap": 2,
                "import_component": 3,
            }.get(r["kind"], 4)
        )
        truncated = len(records) > MAX_RECORDS
        context = {
            "kind": "admission_predictions",
            "tip": tip,
            "records": records[:MAX_RECORDS],
            "ownership": [
                {"pr": m["number"], "paths": [p for p, _ in self.changes.get(m["commit_sha"], [])]} for m in active
            ],
            "complete": not errors and not truncated,
            "retryable": retryable,
            "ownership_complete": all(m["commit_sha"] in self.changes for m in active),
            "errors": errors,
            "truncated": truncated,
            "message": "Admission-time predictions and overlaps; the worker's scripted build and audits decide findings.",
        }
        while len(json.dumps(context).encode()) > MAX_CONTEXT_BYTES:
            context.update(complete=False, truncated=True)
            if context["ownership"]:
                context["ownership_complete"] = False
                del context["ownership"][max(0, len(context["ownership"]) // 2) :]
            elif context["records"]:
                del context["records"][max(0, len(context["records"]) // 2) :]
            else:
                break
        return context


def project_source(path: str) -> bool:
    """A project module path, the inventory the CI import-header preflight checks."""
    return path == "TauCeti.lean" or (path.startswith("TauCeti/") and path.endswith(".lean"))


def import_gaps(planner: Planner, tree: str) -> tuple[frozenset[tuple[str, str]] | None, str | None]:
    """Every project import in `tree` with no source, as `(importer_path, module)`; memoised per tree.

    The inventory is the one the CI import-header preflight checks (regular blobs under `TauCeti/`
    and `TauCeti.lean`); any other kind of entry there makes the scan unavailable, as it does the
    preflight. The tree is far too large to read every header (26,000 files, 270 MiB), so one
    `git grep` finds the lines that import a project module, and only the lines naming a module
    with no source in the inventory, which are rare, are confirmed by parsing that importer's
    header through the planner's blob cache: a match inside a comment or after the header is not
    an import. An importer whose blob the cache omits (oversize or non-text) is unknown, which can
    hide a gap but never invent one. Returns `(None, reason)` when the scan is unavailable.
    """
    if tree in planner.gaps:
        return planner.gaps[tree]
    result: tuple[frozenset[tuple[str, str]] | None, str | None]
    try:
        listing = planner.git("ls-tree", "-r", "-z", tree).decode().split("\0")
        files: dict[str, str] = {}
        reason = None
        for entry in listing:
            if not entry:
                continue
            meta, path = entry.split("\t", 1)
            mode, kind, oid = meta.split()
            if not project_source(path):
                continue
            if kind != "blob" or mode != "100644":
                reason = f"unsupported source entry {path}"
                break
            files[path] = oid
        if reason is not None:
            result = (None, reason)
        else:
            candidates: dict[str, set[str]] = defaultdict(set)
            matches = grep_imports(planner, tree)
            for path, module in matches:
                if path in files and module.replace(".", "/") + ".lean" not in files:
                    candidates[path].add(module)
            if len(candidates) > MAX_GAP_CANDIDATES:
                result = (None, f"{len(candidates)} importers of absent sources; the base itself looks broken")
            else:
                planner.read_headers([files[path] for path in candidates])
                gaps = set()
                for path, modules in candidates.items():
                    header = planner.headers.get(files[path])
                    if header is None:
                        continue  # omitted blob: its imports are unknown
                    gaps.update((path, module) for module in modules if module in header)
                result = (frozenset(gaps), None)
    except (ValueError, UnicodeError, subprocess.SubprocessError) as error:
        result = (None, type(error).__name__ + ": " + str(error)[:200])
    planner.gaps[tree] = result
    while len(planner.gaps) > MAX_GAP_TREES:
        del planner.gaps[next(iter(planner.gaps))]
    return result


def grep_imports(planner: Planner, tree: str) -> list[tuple[str, str]]:
    """`(path, module)` for every line of a project source in `tree` that looks like a project import."""
    try:
        raw = planner.git(
            "grep", "-I", "-z", "--no-color", "-E", "-e", IMPORT_LINE_ERE, tree, "--", "TauCeti/*.lean", "TauCeti.lean"
        )
    except subprocess.CalledProcessError as error:
        if error.returncode == 1 and not error.stderr:
            return []  # no match at all
        raise
    out = []
    prefix = tree + ":"
    for line in raw.decode().split("\n"):
        if "\0" not in line:
            continue
        location, content = line.split("\0", 1)
        if not location.startswith(prefix):
            raise ValueError("unexpected git grep location")
        match = IMPORT_LINE.match(content)
        if match:
            out.append((location[len(prefix) :], match[1]))
    return out


def task_context(context: dict) -> dict:
    """Retain agent evidence without copying the controller's ownership cache/bookkeeping."""
    fields = {"kind", "tip", "records", "complete", "errors", "truncated", "message", "proactive", "was_proactive"}
    return {key: value for key, value in context.items() if key in fields}


def prompt_lines(context: dict | None, *, with_file: bool = False) -> list[str]:
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
            caveat = " (owner list incomplete)" if not record.get("interacting_complete", True) else ""
            lines.append(
                f"- Insertion of #{record['pr']} conflicted with batch {record['batch_id']} at "
                + ", ".join(record["paths"])
                + "; interacting members: "
                + ", ".join(f"#{p}" for p in record["members"])
                + caveat
                + "."
            )
        else:
            lines.append(
                "- Connected import/shared-path component: " + ", ".join(f"#{p}" for p in record["members"]) + "."
            )
    if not context.get("complete", True):
        lines.append("This context is incomplete; missing evidence is unknown.")
    if with_file:
        lines.append("The full retained admission context is in `.tauceti-repair/admission.json`.")
    lines.append("")
    return lines

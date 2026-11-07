#!/usr/bin/env python3
"""Extract reviewable gh contracts from immutable integration-inventory sources.

This is a static discovery tool, never a shell/Python evaluator. Exact argv and
query literals are distinguished from dynamic expressions, wrapper assumptions,
and shell/prompt candidates. No finding by itself proves runtime compatibility.
"""

from __future__ import annotations

import argparse
import ast
import datetime
import hashlib
import importlib.util
import json
import platform
import re
import shlex
import sys
import tomllib
import urllib.parse
from pathlib import Path

SPEC = importlib.util.spec_from_file_location("forge_inventory", Path(__file__).with_name("forge-inventory.py"))
inventory = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(inventory)

COMMANDS = set(
    "api attestation auth browse cache codespace completion config extension gist gpg-key issue label org pr preview project release repo ruleset run search secret ssh-key status variable workflow".split()
)
EXECUTORS = {
    "subprocess.run",
    "subprocess.call",
    "subprocess.check_output",
    "subprocess.check_call",
    "subprocess.Popen",
    "subprocess.getoutput",
    "os.system",
    "os.popen",
    "asyncio.create_subprocess_shell",
    "create_subprocess_shell",
}

# Names identify candidates, not verified wrapper semantics. The source definition
# still needs review: e.g. gh_run takes a full command, while _gh often prefixes gh.
WRAPPERS = {"gh", "_gh", "gh_run", "run_gh", "gh_json", "gh_jsonl", "gh_lines", "_gh_read_only", "proxy_readonly"}
HTTP_WRAPPERS = {"request", "pages", "graphql", "get_json", "paginate", "api"}
ARGUMENT_KEYWORDS = {"args", "argv", "cmd", "command"}
REST_PREFIXES = ("repos/", "users/", "user/", "orgs/", "installation/", "https://api.github.com/")


def endpoint_expression(node):
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value.lstrip("/").startswith(REST_PREFIXES)
    if isinstance(node, ast.JoinedStr) and node.values and isinstance(node.values[0], ast.Constant):
        return endpoint_expression(node.values[0])
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return endpoint_expression(node.left)
    return False


NON_EXECUTORS = {"append", "extend", "join", "writerow", "CompletedProcess"}
QUERY = re.compile(
    r"^\s*(?:(?:query|mutation|subscription)(?:\s+[A-Za-z_]\w*)?\s*[({]|fragment\s+[A-Za-z_]\w*\s+on\s+[A-Za-z_]\w*\s*\{|\{\s*[A-Za-z_]\w*(?:\s*:\s*[A-Za-z_]\w*)?\s*[({])"
)
SHELL_GH = re.compile(r"(?<![\w./-])gh(?=[ \t]+(?:--?[\w-]+|[\w-]+|[$\{]))")
TEXT_GRAPHQL = re.compile(
    r"\b(?:graphql|GraphQL)\b|\b(?:query|mutation|subscription|fragment)\s*(?:[({]|[A-Za-z_]\w*\s*[({])|[\"']query[\"']\s*:"
)
EXECV = {"os.execv", "os.execve", "os.execvp", "os.execvpe"}
VARIADIC_EXECUTORS = {"asyncio.create_subprocess_exec", "create_subprocess_exec"}
SCOPE_TYPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)


def location(node):
    return {
        "line": node.lineno,
        "column": node.col_offset,
        "end_line": node.end_lineno,
        "end_column": node.end_col_offset,
    }


def is_python(path, source):
    first = source.split("\n", 1)[0]
    return path.endswith(".py") or (
        first.startswith("#!") and ("python" in first or re.search(r"\buv\s+run\b.*--script\b", first))
    )


def expression(node):
    return {"python": ast.unparse(node)}


def value(node):
    """A literal or an explicit unevaluated expression; never infer variable values."""
    if isinstance(node, ast.Constant) and isinstance(node.value, (str, int, float, bool, type(None))):
        return node.value
    if isinstance(node, ast.JoinedStr):
        parts = []
        for part in node.values:
            if isinstance(part, ast.Constant):
                parts.append(part.value)
            else:
                # Preserve conversions and format specs rather than inventing the
                # resulting flag, ID, endpoint, JSON body or query syntax.
                parts.append(expression(part))
        return {"format": parts}
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = value(node.left), value(node.right)
        if isinstance(left, str) and isinstance(right, str):
            return left + right
        return {"concat": [left, right]}
    return expression(node)


def argv(node):
    if isinstance(node, (ast.List, ast.Tuple)):
        return [{"spread": ast.unparse(v.value)} if isinstance(v, ast.Starred) else value(v) for v in node.elts]
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return [*argv(node.left), *argv(node.right)]
    return [{"spread": ast.unparse(node)}]


def complete(arguments):
    return all(isinstance(item, str) for item in arguments)


def contract(arguments):
    """Only extract flags/fields from exact argv: dynamic spreads can shift arity."""
    if not complete(arguments):
        return {"resolution": "dynamic", "review": "Resolve expressions and conditional argv construction at source"}
    words = list(arguments)
    if words and words[0] == "gh":
        words.pop(0)
    # Global flags may precede the group. Do not guess their arity or parse a
    # string such as a --repo value as a command.
    if not words or words[0].startswith("-"):
        return {"resolution": "literal", "review": "Resolve global flags or wrapper prefix before parsing"}
    if words[0] not in COMMANDS:
        return {
            "resolution": "literal",
            "group_candidate": words[0],
            "review": "Verify an unknown command group or outer launcher/wrapper prefix",
        }
    result = {"resolution": "literal", "group": words[0]}
    if len(words) > 1 and not words[1].startswith("-"):
        result["endpoint" if words[0] == "api" else "action"] = words[1]
    result["flags"] = sorted({word.split("=", 1)[0] for word in words if word.startswith("-") and word != "-"})
    # A source argv can contain flag-looking data. Flag spellings here are review
    # candidates, not a replacement for gh's command-specific arity parser.
    result["flag_scan"] = "lexical; verify option arity and flag-looking values"
    return result


def python_rows(source):
    try:
        return _python_rows(source)
    except RecursionError:
        return [], [{"reason": "python_analysis_recursion_limit", "line": None}]


def _python_rows(source):
    try:
        tree = ast.parse(source)
    except (SyntaxError, RecursionError) as exc:
        return [], [{"reason": "python_parse_failed", "line": getattr(exc, "lineno", None)}]
    rows = []
    parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}

    def scope(node):
        parent = parents.get(node)
        while parent is not None and not isinstance(parent, SCOPE_TYPES):
            parent = parents.get(parent)
        return parent or tree

    # Same-file, name-based candidate discovery. Descendant references do not
    # establish execution, class dispatch or a Python binding proof.
    functions = [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    summaries = []
    for function in functions:
        calls = {ast.unparse(n.func).rsplit(".", 1)[-1] for n in ast.walk(function) if isinstance(n, ast.Call)}
        gh_literal = any(isinstance(n, ast.Constant) and n.value == "gh" for n in ast.walk(function))
        summaries.append((function, calls, gh_literal))
    derived = {}
    changed = True
    while changed:
        changed = False
        for function, calls, gh_literal in summaries:
            if gh_literal or calls & (WRAPPERS | HTTP_WRAPPERS | set(derived)):
                entry = {"name": function.name, **location(function)}
                definitions = derived.setdefault(function.name, [])
                if entry not in definitions:
                    definitions.append(entry)
                    changed = True

    bindings = {}
    command_names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            names = [t.id for t in node.targets if isinstance(t, ast.Name)]
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign, ast.NamedExpr)) and isinstance(node.target, ast.Name):
            names = [node.target.id]
        else:
            continue
        if node.value is None:
            continue
        for name in names:
            definition = {
                "name": name,
                "kind": type(node).__name__,
                **location(node),
                "value": argv(node.value)
                if isinstance(node.value, (ast.List, ast.Tuple, ast.BinOp))
                else value(node.value),
            }
            bindings.setdefault((scope(node), name), []).append(definition)
            for child in ast.walk(node.value):
                if isinstance(child, (ast.List, ast.Tuple)) and child.elts and isinstance(child.elts[0], ast.Constant):
                    first = child.elts[0].value
                    if isinstance(first, str) and first in COMMANDS | {"gh"}:
                        command_names.add((scope(node), name))

    def lookup(node, name):
        current = scope(node)
        while True:
            key = (current, name)
            if key in bindings:
                return bindings[key], key in command_names
            if current is tree:
                return [], False
            current = scope(current)
            while isinstance(current, ast.ClassDef):
                current = scope(current)

    for key, definitions in bindings.items():
        if key in command_names:
            for definition in definitions:
                rows.append(
                    {
                        **{k: definition[k] for k in ("line", "column", "end_line", "end_column")},
                        "kind": "argv_assignment_candidate",
                        "name": definition["name"],
                        "assignment": definition["kind"],
                        "value": definition["value"],
                    }
                )

    for node in ast.walk(tree):
        if (
            isinstance(node, (ast.List, ast.Tuple))
            and node.elts
            and isinstance(node.elts[0], ast.Constant)
            and node.elts[0].value == "gh"
        ):
            arguments = argv(node)
            rows.append({**location(node), "kind": "argv_literal", "argv": arguments, "contract": contract(arguments)})
        if isinstance(node, ast.Call):
            callee = ast.unparse(node.func)
            name = callee.rsplit(".", 1)[-1]
            call_args = list(node.args)
            keywords = {kw.arg or "**": value(kw.value) for kw in node.keywords}
            all_arguments = [*node.args, *(kw.value for kw in node.keywords)]
            if not call_args:
                call_args = [kw.value for kw in node.keywords if kw.arg in ARGUMENT_KEYWORDS]
            if not call_args and name in HTTP_WRAPPERS:
                call_args = [kw.value for kw in node.keywords]
            if not call_args and name in derived:
                rows.append(
                    {
                        **location(node),
                        "kind": "derived_wrapper_candidate",
                        "callee": callee,
                        "positional_arguments": [],
                        "keyword_arguments": keywords,
                        "wrapper_definitions": derived[name],
                        "review": "Verify same-file wrapper name, dispatch, defaults and execution context",
                    }
                )
                continue
            if not call_args:
                continue
            selected = call_args[1:2] if callee in EXECV else call_args
            if not selected:
                continue
            first = selected[0]
            variadic = callee in VARIADIC_EXECUTORS or (
                callee not in EXECUTORS | EXECV
                and (
                    isinstance(first, (ast.Constant, ast.JoinedStr, ast.Starred))
                    or len(selected) > 1
                    and not isinstance(first, (ast.List, ast.Tuple, ast.BinOp))
                )
            )
            arguments = (
                [({"spread": ast.unparse(v.value)} if isinstance(v, ast.Starred) else value(v)) for v in selected]
                if variadic
                else argv(first)
            )
            references, seen_names, linked = [], set(), False
            for argument in all_arguments:
                for part in ast.walk(argument):
                    if isinstance(part, ast.Name) and part.id not in seen_names:
                        seen_names.add(part.id)
                        definitions, command_binding = lookup(node, part.id)
                        references.extend(definitions)
                        linked = linked or command_binding
            starts_gh = arguments and arguments[0] == "gh"
            if (
                name in {"append", "extend", "insert"}
                and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and lookup(node, node.func.value.id)[1]
            ):
                kind = "argv_extension_candidate"
            elif name in HTTP_WRAPPERS:
                kind = "http_wrapper_candidate"
            elif name in NON_EXECUTORS:
                continue
            elif callee in EXECUTORS | EXECV | VARIADIC_EXECUTORS:
                if starts_gh:
                    kind = "direct_argv" if callee in EXECUTORS else "exec_argv_candidate"
                elif any(item == "gh" for item in arguments):
                    kind = "embedded_argv_candidate"
                elif arguments and not isinstance(arguments[0], str):
                    kind = "dynamic_executor_candidate"
                else:
                    continue
            elif name in WRAPPERS:
                kind = "wrapper_candidate"
            elif starts_gh:
                kind = "executor_candidate"
            elif name in derived:
                kind = "derived_wrapper_candidate"
            elif endpoint_expression(first):
                kind = "endpoint_call_candidate"
            elif name == "read" and arguments and isinstance(arguments[0], str) and arguments[0] in COMMANDS:
                kind = "callback_candidate"
            elif isinstance(first, ast.Constant) and isinstance(first.value, str) and first.value in COMMANDS:
                kind = "command_group_candidate"
            elif (
                isinstance(first, (ast.List, ast.Tuple))
                and first.elts
                and isinstance(first.elts[0], ast.Constant)
                and isinstance(first.elts[0].value, str)
                and first.elts[0].value in COMMANDS
            ):
                kind = "command_group_candidate"
            elif linked:
                kind = "linked_executor_candidate"
            else:
                continue
            row = {**location(node), "kind": kind, "callee": callee, "argv": arguments, "contract": contract(arguments)}
            if kind in {"http_wrapper_candidate", "derived_wrapper_candidate", "endpoint_call_candidate"}:
                row.pop("argv")
                row.pop("contract")
                row["positional_arguments"] = [value(v) for v in node.args]
                row["review"] = "Verify HTTP-wrapper method/path/body semantics and source forge or other provider"
            if name in derived:
                row["wrapper_definitions"] = derived[name]
            if kind not in {"direct_argv", "exec_argv_candidate"} and "contract" in row and "group" in row["contract"]:
                row["contract"]["group_candidate"] = row["contract"].pop("group")
            if keywords:
                row["keyword_arguments"] = keywords
            if callee in EXECV:
                row["executable"] = value(call_args[0])
            if references:
                # These are syntactic definitions in lexical/enclosing scopes,
                # not a control-flow or reaching-definition proof.
                row["binding_sources"] = references
            rows.append(row)
        if isinstance(node, (ast.Constant, ast.JoinedStr)):
            if isinstance(parents.get(node), ast.JoinedStr):
                continue
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                text = node.value
            elif isinstance(node, ast.JoinedStr):
                text = "".join(
                    p.value if isinstance(p, ast.Constant) else "{" + ast.unparse(p.value) + "}" for p in node.values
                )
            else:
                continue
            for candidate in text_rows(text, prompt=True):
                if candidate["kind"] != "prompt_candidate":
                    continue
                rows.append(
                    {
                        **location(node),
                        "kind": "python_string_candidate",
                        "template_line": candidate["line"],
                        "template_column": candidate["column"],
                        "text": candidate["text"],
                        "template": value(node),
                        "review": "Establish generation/execution context and resolve expressions",
                    }
                )
            query_text = text.removeprefix("query=")
            # A formatted Python value such as f"{name} failed" is not an
            # anonymous GraphQL operation. Its root field must start in literal
            # text, not in the placeholder notation used for display above.
            literal_root = not isinstance(node, ast.JoinedStr) or (
                node.values
                and isinstance(node.values[0], ast.Constant)
                and QUERY.match(node.values[0].value.removeprefix("query="))
            )
            if "api.github.com" in text or "/graphql" in text:
                rows.append(
                    {
                        **location(node),
                        "kind": "http_endpoint_literal",
                        "text": text,
                        "review": "Trace the direct HTTP provider, method, body and credential scope",
                    }
                )
            if (QUERY.match(query_text) or text.startswith("query=")) and literal_root:
                parent = parents.get(node)
                composition = (
                    "dynamic_fstring"
                    if isinstance(node, ast.JoinedStr)
                    else "string_piece"
                    if isinstance(parent, ast.BinOp)
                    else "standalone_literal"
                )
                row = {
                    **location(node),
                    "kind": "graphql_literal",
                    "query": query_text if isinstance(node, ast.Constant) else value(node),
                    "composition": composition,
                }
                if text.startswith("query="):
                    row["field_prefix"] = "query="
                rows.append(row)
    return sorted(rows, key=lambda r: (r["line"], r["column"], r["kind"])), []


def text_rows(source, *, prompt=False):
    rows = []
    lines = source.split("\n")
    index = 0
    while index < len(lines):
        line = lines[index]
        for match in SHELL_GH.finditer(line):
            candidate = line[match.start() :]
            end = index
            while True:
                try:
                    tokens = shlex.split(candidate.replace("\\\n", "").replace("\\\r\n", ""), comments=True)
                    quoted = False
                except ValueError as exc:
                    tokens = None
                    quoted = "No closing quotation" in str(exc)
                continued = candidate.rstrip("\r").endswith("\\")
                if not ((quoted and not prompt) or continued) or end + 1 >= len(lines) or end - index >= 511:
                    break
                end += 1
                candidate += "\n" + lines[end]
            rows.append(
                {
                    "line": index + 1,
                    "column": len(line[: match.start()].encode()),
                    "end_line": end + 1,
                    "end_column": len(lines[end].encode()),
                    "kind": "prompt_candidate" if prompt else "shell_candidate",
                    "text": candidate,
                    "tokens": tokens,
                    "review": "Verify shell boundaries, substitutions, here-docs and execution context",
                }
            )
        # Non-Python query builders/direct HTTP clients need language-specific
        # review. A line candidate does not claim a complete query or execution.
        if not prompt and TEXT_GRAPHQL.search(line):
            rows.append(
                {
                    "line": index + 1,
                    "column": 0,
                    "end_line": index + 1,
                    "end_column": len(line.encode()),
                    "kind": "graphql_text_candidate",
                    "text": line,
                    "review": "Extract the language-specific query and trace its HTTP/call context",
                }
            )
        index += 1
    return rows


def identify(row, name, commit, role):
    payload = {
        k: row[k]
        for k in (
            "callee",
            "executable",
            "argv",
            "query",
            "text",
            "template",
            "template_line",
            "template_column",
            "field_prefix",
            "positional_arguments",
            "keyword_arguments",
            "name",
            "assignment",
            "value",
        )
        if k in row
    }
    source = {k: row[k] for k in ("blob", "kind", "line", "column", "end_line", "end_column")}
    content_id = hashlib.sha256(json.dumps([source, payload], sort_keys=True).encode()).hexdigest()
    occurrence = [name, commit, role, row["path"], content_id]
    return {**row, "content_id": content_id, "id": hashlib.sha256(json.dumps(occurrence).encode()).hexdigest()}


def inspect(name, root, revision, *, role="inventory", expected=None, provenance=None):
    baseline = inventory.inspect(name, root, revision)
    if expected is not None and any(baseline[key] != expected[key] for key in ("source_revision", "files", "skipped")):
        raise ValueError(f"inventory file selection changed for {name}")
    # Expand the inventory's PATH policy independently of its lexical hits, so a
    # wrapper-only Python module cannot be missed because it lacks literal gh.
    snapshot = inventory.inspect(name, root, revision, include_unmatched=True)
    commit = snapshot["source_revision"]
    rows, skipped, scanned_files = [], list(snapshot["skipped"]), []
    blobs = {}
    for entry in inventory.git(root, "ls-tree", "-rz", commit).split(b"\0"):
        if entry:
            metadata, raw_path = entry.split(b"\t", 1)
            mode, kind, oid = metadata.split()
            if kind == b"blob":
                blobs[raw_path.decode(errors="surrogateescape")] = oid.decode()
    for item in snapshot["files"]:
        path = item["path"]
        blob = blobs[path]
        data = inventory.git(root, "cat-file", "blob", blob)
        source = data.decode()
        language = "python" if is_python(path, source) else "text"
        if language == "python":
            findings, errors = python_rows(source)
            skipped.extend({"path": path, **error} for error in errors)
            if errors:
                findings.extend(text_rows(source))
                language = "python_parse_failed_text_fallback"
        else:
            findings = text_rows(source, prompt=path.endswith(".md"))
        lines = source.split("\n")
        hits = {}
        for category in ("gh_cli", "graphql"):
            for number in item["categories"].get(category, []):
                hits.setdefault(number, []).append(category)
        for number, line in enumerate(lines, 1):
            if "api.github.com" in line:
                hits.setdefault(number, []).append("github_http")
        for number, categories in sorted(hits.items()):
            if not any(
                r["line"] <= number <= (r["line"] if "tokens" in r and r["tokens"] is None else r["end_line"])
                for r in findings
            ):
                findings.append(
                    {
                        "line": number,
                        "column": 0,
                        "end_line": number,
                        "end_column": len(lines[number - 1].encode()),
                        "kind": "uncovered_inventory_hit",
                        "categories": sorted(set(categories)),
                        "text": lines[number - 1],
                        "review": "Manual classification required; no command/query extraction covers this source hit",
                    }
                )
        for row in sorted(findings, key=lambda r: (r["line"], r["column"], r["kind"])):
            rows.append(identify({"path": path, "blob": blob, **row}, name, commit, role))
        scanned_files.append({"path": path, "blob": blob, "language": language, "records": len(findings)})
    return {
        "repository": name,
        "source_revision": commit,
        "role": role,
        "provenance": provenance or {"kind": "inventory_revision"},
        "records": rows,
        "scanned_files": scanned_files,
        "skipped": skipped,
    }


def pin_provenance(pin, roots):
    origin = pin["provenance"]
    if not isinstance(origin, dict):
        raise ValueError("pin provenance must be an object")
    if origin.get("kind") == "git_dependency":
        if set(origin) != {"kind", "repository", "revision", "path", "blob", "dependency"}:
            raise ValueError("dependency provenance contains unknown fields")
        root = roots[origin["repository"]]
        revision = origin["revision"]
        if not re.fullmatch(r"[a-f0-9]{40}|[a-f0-9]{64}", revision) or origin["path"] != "pyproject.toml":
            raise ValueError("dependency provenance requires an immutable pyproject.toml source")
        data = inventory.git(root, "show", revision + ":pyproject.toml")
        blob = inventory.git(root, "rev-parse", revision + ":pyproject.toml").decode().strip()
        if (
            blob != origin["blob"]
            or origin["dependency"] not in tomllib.loads(data.decode())["project"]["dependencies"]
        ):
            raise ValueError("dependency provenance does not match its source blob")
        dependency = origin["dependency"]
        if " @ git+" not in dependency:
            raise ValueError("dependency source is not a pinned Git URL")
        url, sep, declared = dependency.split(" @ git+", 1)[1].rpartition("@")
        parsed = urllib.parse.urlsplit(url)
        if (
            not sep
            or declared != pin["revision"]
            or parsed.username
            or parsed.password
            or parsed.scheme != "https"
            or parsed.path.rsplit("/", 1)[-1].removesuffix(".git").casefold() != pin["repository"].casefold()
        ):
            raise ValueError("dependency target does not match the requested source pin")
        return {**origin, "validation": "matched immutable source dependency"}
    if origin.get("kind") == "runtime_marker":
        if set(origin) != {"kind", "observation", "observation_sha256"}:
            raise ValueError("runtime marker provenance contains unknown fields")
        observation = origin["observation"]
        expected = {
            "observed_at",
            "target",
            "field",
            "revision",
            "running_processes_verified",
            "parent_observation_sha256",
        }
        if (
            not isinstance(observation, dict)
            or set(observation) != expected
            or observation["running_processes_verified"] is not False
        ):
            raise ValueError("runtime-marker observation must explicitly leave running processes unverified")
        if any(not isinstance(observation[key], str) for key in expected - {"running_processes_verified"}):
            raise ValueError("runtime-marker observation fields must be strings")
        when = datetime.datetime.fromisoformat(observation["observed_at"])
        if (
            when.tzinfo is None
            or observation["revision"] != pin["revision"]
            or not observation["target"]
            or not observation["field"]
            or not re.fullmatch(r"[a-f0-9]{64}", observation["parent_observation_sha256"])
        ):
            raise ValueError("runtime-marker observation does not bind a timestamp, target and source pin")
        digest = hashlib.sha256(json.dumps(observation, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        if origin["observation_sha256"] != digest:
            raise ValueError("runtime-marker observation receipt digest mismatch")
        return {
            **origin,
            "validation": "operator-supplied timestamped source-marker receipt; running processes not verified",
        }
    if origin == {"kind": "operator_argument"}:
        return origin
    raise ValueError("unknown pin provenance kind")


def build_report(document, inventory_bytes, roots, pins):
    if not isinstance(document, dict) or any(
        not isinstance(pin, dict) or not isinstance(pin.get("provenance"), dict) for pin in pins
    ):
        raise ValueError("inventory and pin provenance must be objects")
    selector_hash = hashlib.sha256(Path(inventory.__file__).read_bytes()).hexdigest()
    if document.get("schema") != "tauceti.forge-inventory/v1" or document.get("scanner_sha256") != selector_hash:
        raise ValueError("inventory scanner digest mismatch; replay the inventory with the current selector first")
    reports, names, seen_pins = [], set(), set()
    requested_pins = {(pin["repository"], pin["revision"]) for pin in pins}
    for snapshot in document["repositories"]:
        name = snapshot["repository"]
        if name not in roots or name in names:
            raise ValueError(f"missing or duplicate inventory checkout: {name}")
        names.add(name)
        reports.append(inspect(name, roots[name], snapshot["source_revision"], expected=snapshot))
    for pin in pins:
        name, revision = pin["repository"], pin["revision"]
        if name not in roots or not re.fullmatch(r"[a-f0-9]{40}|[a-f0-9]{64}", revision):
            raise ValueError("pin must name a supplied checkout and full commit")
        if (
            inventory.git(roots[name], "rev-parse", revision + "^{commit}").decode().strip() != revision
            or inventory.git(roots[name], "cat-file", "-t", revision).decode().strip() != "commit"
        ):
            raise ValueError("source pin must identify a commit object, not a tag")
        if (name, revision) in seen_pins:
            raise ValueError(f"duplicate source pin: {name}@{revision}")
        seen_pins.add((name, revision))
        origin = pin["provenance"]
        if origin.get("kind") == "git_dependency" and (origin["repository"], origin["revision"]) not in requested_pins:
            raise ValueError("dependency provenance is not bound to an inspected source pin")
        provenance = pin_provenance(pin, roots)
        reports.append(inspect(name, roots[name], revision, role="declared_pin", provenance=provenance))
    all_ids = [row["id"] for report in reports for row in report["records"]]
    if len(all_ids) != len(set(all_ids)):
        raise ValueError("discovery occurrence IDs collided")
    return {
        "schema": "tauceti.forge-contract-candidates/v1",
        "scanner_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "inventory_sha256": hashlib.sha256(inventory_bytes).hexdigest(),
        "inventory_scanner_sha256": selector_hash,
        "runtime": {
            "python": sys.version,
            "implementation": platform.python_implementation(),
            "git": inventory.git(next(iter(roots.values())), "--version").decode().strip(),
        },
        "policy": {
            "executors": sorted(EXECUTORS | EXECV | VARIADIC_EXECUTORS),
            "wrapper_candidate_names": sorted(WRAPPERS),
            "non_executors": sorted(NON_EXECUTORS),
            "http_wrapper_names": sorted(HTTP_WRAPPERS),
            "argument_keywords": sorted(ARGUMENT_KEYWORDS),
            "command_group_candidates": sorted(COMMANDS),
            "rest_prefix_candidates": list(REST_PREFIXES),
            "derived_wrappers": "same-file function-name fixed point over descendant gh literals and known/derived wrapper calls; candidate dispatch only",
            "read_callback": "name read with a literal command-group prefix",
            "line_scanning": "every physical line; unmatched quote ranges only cover their first line for reconciliation",
            "selection": "integration-inventory path policy, including eligible sources without lexical hits",
            "python": "AST call/literal/binding discovery; .py and recognized Python/uv-script shebangs",
            "other_languages": "gh snippets and GraphQL text candidates only; language-specific query extraction requires review",
            "columns": "zero-based UTF-8 byte offsets; Python template coordinates are within decoded literal text",
            "shell_max_lines": 512,
        },
        "scope": "Eligible tracked sources at inventory revisions plus explicit source pins. No installed runtime, credential or environment files are read.",
        "limitations": "Static discovery, not an accepted golden corpus or coverage proof. Bindings are candidate syntactic definitions in lexical/enclosing scopes, not reaching-definition or execution evidence. Wrapper/callback/executor semantics, shell boundaries, here-docs, language-specific GraphQL composition, direct HTTP clients and dynamic argv require manual review. Runtime-only construction and unrecognized launch mechanisms can escape extraction. Pins are source inputs, not verification of all installed artifacts or running processes.",
        "snapshots": reports,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inventory", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--pin", action="append", default=[], metavar="NAME=COMMIT", help="Additional operator-selected source pin"
    )
    parser.add_argument("--pin-manifest", type=Path, help="Source pins with dependency/observation provenance")
    parser.add_argument("repositories", nargs="+", metavar="NAME=/checkout")
    args = parser.parse_args()
    roots = {}
    for spec in args.repositories:
        name, sep, root = spec.partition("=")
        if not sep or not name or not root or name in roots:
            parser.error("each checkout must be a unique NAME=/checkout")
        roots[name] = Path(root)
    try:
        inventory_bytes = args.inventory.read_bytes()
        document = json.loads(inventory_bytes)
        pins = []
        if args.pin_manifest:
            pin_manifest_bytes = args.pin_manifest.read_bytes()
            manifest = json.loads(pin_manifest_bytes)
            if manifest.get("schema") != "tauceti.forge-contract-pins/v1":
                raise ValueError("unsupported pin manifest schema")
            pins.extend(manifest["pins"])
        for declaration in args.pin:
            name, sep, revision = declaration.partition("=")
            if not sep:
                raise ValueError("pin must be NAME=FULL_COMMIT")
            pins.append({"repository": name, "revision": revision, "provenance": {"kind": "operator_argument"}})
        result = build_report(document, inventory_bytes, roots, pins)
        if args.pin_manifest:
            result["pin_manifest_sha256"] = hashlib.sha256(pin_manifest_bytes).hexdigest()
    except (OSError, KeyError, TypeError, ValueError, RuntimeError) as exc:
        parser.error(str(exc))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    for report in result["snapshots"]:
        print(
            f"{report['repository']}@{report['source_revision'][:12]} ({report['role']}): {len(report['records'])} records"
        )


if __name__ == "__main__":
    main()

"""The environment lint's scope for the worker's verification: the changed closure, or the full library.

The full environment lint took 1,362 s on task 1263's tree (33,755 modules), 1,126 s of it in the
linters: ``checkType`` 721 s, ``simpNF`` 313 s, ``unusedArguments`` 32 s, ``synTaut`` 18 s, the other
ten under 10 s each. When a commit ``R`` passed the lint, a later tree only needs what could have
changed since, linter by linter:

* **Local linters** (``checkType``, ``synTaut``, ``unusedArguments``, ``unusedHavesSuffices``,
  ``defsWithUnderscore``, ``structureInType``, ``deprecatedNoSince``, ``impossibleInstance``,
  ``nonClassInstance``, ``subsetDotNotationLinter``) judge a declaration by its own type and value,
  the constants those reach and its own attributes (instance, deprecated, ``nolint``). All of these come
  from its module and the modules it imports transitively, except attributes added later by another
  module (``attribute [..] d``, ``to_additive existing``), which only a module importing ``d`` can do.
  Their verdicts can only change on the *changed closure*: the TauCeti modules whose source changed
  since ``R`` and their transitive importers in the tree's import graph. A changed file that adds or
  removes an ``attribute``, ``existing`` or ``add_decl_doc`` line could change a declaration outside
  the closure, so it means the full lint.
* **Environment-wide linters** (``GLOBAL_LINTERS``: ``simpNF``, ``simpComm``, ``tacticAlt``,
  ``tacticDocs``) consult every simp lemma, instance or tactic in the environment, so a change anywhere
  can change their verdict on any declaration. They always run over the whole library.

So with a changed scope the worker runs TauCetiTools ``lint-env.sh`` (unchanged, in staging's batch
mode ``LINT_CHANGED_ONLY=0``: every module imported, every linter on the listed modules,
``unusedArguments`` also on their importers, the docstring scan and the ``nolint`` allowlist over the
whole library) on the closure, then the pinned driver's environment-wide linters over every module,
read with ``lint-env.sh``'s report grammar and its baseline (``repair_audit.global_lint``). Together they
flag what the full lint would flag, and so everything staging flags (staging's scope is the full
library or, for GitHub batches, the modules changed since the base). If the second report cannot be
read, the full ``lint-env.sh`` runs instead.

``R`` is a commit this worker verified, recorded in ``lint-passes.jsonl`` under the TauCetiTools root
once the full lint, or a scoped lint against an earlier ``R``, passed on it. A record is only usable
under the same lint key: the exact TauCetiTools commit, toolchain and manifest (``driver_key``), the
materialized scripts with the project's baseline and allowlist, and this module's ``VERSION``.

The full lint runs whenever the scope is in doubt: no usable record, uncommitted changes, a change to
anything but TauCeti modules and documentation (pins, toolchain, Lake configuration, scripts, Lean
files outside the library), a changed attribute line as above, an import header this module cannot
read with certainty, or a closure that is the whole library anyway. ``TAUCETI_QUEUE_SCOPED_LINT=0``
forces the full lint.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import subprocess
import time
from pathlib import Path

VERSION = 1
# Batteries/Mathlib environment linters whose verdict on a declaration can depend on declarations it
# does not import: simpNF runs simp with every simp lemma, simproc and congruence lemma, simpComm
# unifies with metavariables (instance arguments may be synthesized from every instance), and
# tacticDocs/tacticAlt compare a tactic with every tactic's documentation and alternatives. With a
# changed scope these run over the whole library; the rest only on the scope (see the docstring).
GLOBAL_LINTERS = ("simpNF", "simpComm", "tacticAlt", "tacticDocs")
PASSES_FILE = "lint-passes.jsonl"
ENABLE_VARIABLE = "TAUCETI_QUEUE_SCOPED_LINT"
MODULE_PATH = re.compile(r"^TauCeti(/[A-Za-z_][A-Za-z0-9_']*)+\.lean$")
ROOT = "TauCeti"
# Paths outside the library whose changes cannot reach the lint (the worker's lint reads only the
# pinned scripts, the worktree's scripts/ policy files, the Lake pins and the TauCeti modules).
IGNORED_PREFIXES = ("docs/", "web/", "assets/")
IGNORED_SUFFIXES = (".md",)
CANDIDATES = 20  # most recent matching records considered as the reference
KEEP = 200  # records retained when the file is compacted
COMPACT_AT = 400
# Changed lines that may set an attribute or docstring on another module's declaration
# (``attribute [..]``, ``to_additive existing``, ``add_decl_doc``). Matching the bare words over-matches
# (a comment saying "existing"), which only costs a full lint.
ATTRIBUTE_LINE = re.compile(r"\battribute\b|\bexisting\b|\badd_decl_doc\b")
IMPORT_AFTER_HEADER = re.compile(r"(?m)^[ \t]*(?:public[ \t]+)?(?:meta[ \t]+)?import[ \t]")


def enabled(environ: dict | None = None) -> bool:
    value = (os.environ if environ is None else environ).get(ENABLE_VARIABLE, "1").strip().lower()
    return value not in ("0", "false", "no", "off")


def _git(worktree: Path, *args: str, timeout: float = 120) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(worktree), *args], capture_output=True, check=False, timeout=timeout)


def lint_key(driver_key: str, scripts: Path) -> str:
    """What a recorded pass is valid under: this module's version, the driver key and every
    materialized script and policy file."""
    digest = hashlib.sha256(f"tauceti-lint-scope/{VERSION}\0{driver_key}\0".encode())
    for path in sorted(p for p in scripts.rglob("*") if p.is_file()):
        digest.update(str(path.relative_to(scripts)).encode() + b"\0")
        digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def snapshot(worktree: Path) -> str | None:
    """HEAD when the worktree has no uncommitted or untracked change (ignored files aside), else None."""
    try:
        status = _git(worktree, "status", "--porcelain", "-z", "--untracked-files=all")
        head = _git(worktree, "rev-parse", "--verify", "HEAD^{commit}")
    except (OSError, subprocess.TimeoutExpired):
        return None
    if status.returncode != 0 or status.stdout or head.returncode != 0:
        return None
    sha = head.stdout.decode().strip()
    return sha if re.fullmatch(r"[0-9a-f]{40}", sha) else None


def _tree(worktree: Path, commit: str) -> str | None:
    try:
        proc = _git(worktree, "rev-parse", "--verify", "--quiet", f"{commit}^{{tree}}")
    except (OSError, subprocess.TimeoutExpired):
        return None
    return proc.stdout.decode().strip() if proc.returncode == 0 else None


# ----------------------------------------------------------------------------------------------
# Recorded passes


def read_passes(passes: Path, key: str) -> list[dict]:
    """The usable-looking records under ``key``, newest first; damaged lines are skipped."""
    try:
        lines = passes.read_text().splitlines()
    except OSError:
        return []
    found = []
    for line in reversed(lines):
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if (
            isinstance(entry, dict)
            and entry.get("key") == key
            and isinstance(entry.get("commit"), str)
            and re.fullmatch(r"[0-9a-f]{40}", entry["commit"])
            and isinstance(entry.get("tree"), str)
        ):
            found.append(entry)
            if len(found) >= CANDIDATES:
                break
    return found


def record_pass(passes: Path, worktree: Path, commit: str, key: str, scope: str) -> bool:
    """Append that ``commit`` passed the lint under ``key``; compacts the file now and then."""
    tree = _tree(worktree, commit)
    if tree is None:
        return False
    entry = {"commit": commit, "tree": tree, "key": key, "scope": scope, "at": round(time.time(), 1)}
    try:
        passes.parent.mkdir(parents=True, exist_ok=True)
        with passes.open("a+") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            handle.seek(0)
            lines = handle.read().splitlines()
            if len(lines) >= COMPACT_AT:
                handle.seek(0)
                handle.truncate()
                handle.write("".join(line + "\n" for line in lines[-KEEP:]))
            handle.write(json.dumps(entry) + "\n")
    except OSError:
        return False
    return True


# ----------------------------------------------------------------------------------------------
# The import graph


def _skip_space_and_comments(text: str, i: int) -> int | None:
    """Index of the next token at or after ``i``; None on an unterminated block comment."""
    n = len(text)
    while i < n:
        if text[i].isspace():
            i += 1
        elif text.startswith("--", i):
            end = text.find("\n", i)
            i = n if end < 0 else end + 1
        elif text.startswith("/-", i):
            depth, i = 1, i + 2
            while depth:
                opening, closing = text.find("/-", i), text.find("-/", i)
                if closing < 0:
                    return None
                if 0 <= opening < closing:
                    depth, i = depth + 1, opening + 2
                else:
                    depth, i = depth - 1, closing + 2
        else:
            return i
    return i


def _token(text: str, i: int) -> tuple[str, int]:
    j = i
    while j < len(text) and not text[j].isspace() and not text.startswith(("--", "/-"), j):
        j += 1
    return text[i:j], j


def header_imports(text: str) -> list[str] | None:
    """The modules a Lean file's header imports (``module``, ``prelude``, then any number of
    ``[public] [meta] import [all] Name``), or None when the header cannot be read with certainty:
    an unterminated comment, a quoted name, or an import-looking line after the header."""
    imports: list[str] = []
    i = _skip_space_and_comments(text, 0)
    if i is None:
        return None
    for keyword in ("module", "prelude"):
        token, j = _token(text, i)
        if token == keyword:
            i = _skip_space_and_comments(text, j)
            if i is None:
                return None
    while True:
        start = i
        token, j = _token(text, i)
        for modifier in ("public", "meta"):
            if token == modifier:
                i = _skip_space_and_comments(text, j)
                if i is None:
                    return None
                token, j = _token(text, i)
        if token != "import":
            i = start
            break
        i = _skip_space_and_comments(text, j)
        if i is None:
            return None
        token, j = _token(text, i)
        if token == "all":
            i = _skip_space_and_comments(text, j)
            if i is None:
                return None
            token, j = _token(text, i)
        if not token or "«" in token or "»" in token:
            return None
        imports.append(token)
        i = _skip_space_and_comments(text, j)
        if i is None:
            return None
    if _import_after(text, i):
        return None
    return imports


def _import_after(text: str, i: int) -> bool:
    """Whether a line outside comments, from ``i`` on, starts like an import. Lean rejects an import
    after the header, so in a file that builds this flags a header this module misread."""
    while i < len(text):
        opening = text.find("/-", i)
        segment = text[i : len(text) if opening < 0 else opening]
        # A segment may start mid-line, after a comment; that can only flag too much.
        if IMPORT_AFTER_HEADER.search(segment):
            return True
        if opening < 0:
            return False
        i = _skip_space_and_comments(text, opening)
        if i is None:
            return True
    return False


def import_graph(worktree: Path) -> tuple[dict[str, set[str]], str] | tuple[None, str]:
    """``{module: TauCeti modules it imports}`` over every tracked TauCeti module, or None and why."""
    try:
        listed = _git(worktree, "ls-files", "-z", "--", ROOT)
    except (OSError, subprocess.TimeoutExpired) as error:
        return None, f"listing the library: {error}"
    if listed.returncode != 0:
        return None, "listing the library failed"
    graph: dict[str, set[str]] = {}
    for raw in listed.stdout.split(b"\0"):
        path = raw.decode("utf-8", "surrogateescape")
        if not path.endswith(".lean"):
            continue
        if not MODULE_PATH.match(path):
            return None, f"a Lean file the module list cannot name: {path}"
        try:
            text = (worktree / path).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as error:
            return None, f"reading {path}: {error}"
        imports = header_imports(text)
        if imports is None:
            return None, f"the import header of {path} could not be read with certainty"
        graph[path[: -len(".lean")].replace("/", ".")] = {m for m in imports if m.startswith(ROOT + ".")}
    if not graph:
        return None, "no TauCeti modules"
    return graph, ""


def reverse_closure(graph: dict[str, set[str]], seeds: set[str]) -> set[str]:
    importers: dict[str, list[str]] = {}
    for module, imports in graph.items():
        for imported in imports:
            importers.setdefault(imported, []).append(module)
    seen = set(seeds)
    pending = list(seeds)
    while pending:
        for importer in importers.get(pending.pop(), ()):
            if importer not in seen:
                seen.add(importer)
                pending.append(importer)
    return seen


# ----------------------------------------------------------------------------------------------
# The scope


def _changes(worktree: Path, reference: str) -> list[tuple[str, str]] | None:
    try:
        proc = _git(
            worktree, "diff", "--no-ext-diff", "--no-textconv", "--no-renames", "--name-status", "-z", reference, "HEAD"
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    fields = proc.stdout.decode("utf-8", "surrogateescape").split("\0")
    if fields and fields[-1] == "":
        fields.pop()
    if len(fields) % 2:
        return None
    return list(zip(fields[0::2], fields[1::2], strict=True))


def _ignorable(path: str) -> bool:
    if path.endswith(".lean"):
        return False
    return path.startswith(IGNORED_PREFIXES) or path.endswith(IGNORED_SUFFIXES)


def full(reason: str, **extra) -> dict:
    return {"scope": "full", "reason": reason, **extra}


def choose(worktree: Path, *, head: str | None, passes: Path, key: str) -> dict:
    """``{"scope": "changed", "modules": [...], "reference": sha, ...}`` or ``{"scope": "full", "reason"}``;
    ``head`` is :func:`snapshot`'s value."""
    if not enabled():
        return full(f"{ENABLE_VARIABLE}=0")
    if head is None:
        return full("the worktree has uncommitted changes")
    best = None
    reasons = []
    for entry in read_passes(passes, key):
        commit = entry["commit"]
        if _tree(worktree, commit) != entry["tree"]:
            continue  # pruned, or not this repository's commit
        if commit == head:
            best = (entry, [])
            break
        changes = _changes(worktree, commit)
        if changes is None:
            continue
        other = [path for _, path in changes if not MODULE_PATH.match(path) and not _ignorable(path)]
        if other:
            reasons.append(f"{commit[:12]}: {other[0]} changed")
            continue
        if best is None or len(changes) < len(best[1]):
            best = (entry, changes)
    if best is None:
        return full(
            "no verified lint pass to compare against"
            if not reasons
            else "every verified pass differs in " + "; ".join(reasons[:3])
        )
    entry, changes = best
    reference = entry["commit"]
    paths = [path for _, path in changes if MODULE_PATH.match(path)]
    attribute = _attribute_change(worktree, reference, paths)
    if attribute:
        return full(attribute, reference=reference)
    graph, why = import_graph(worktree)
    if graph is None:
        return full(why, reference=reference)
    changed = {path[: -len(".lean")].replace("/", ".") for path in paths}
    seeds = {m for m in changed if m in graph}
    # A deleted module's importers changed too, or the build failed; take them anyway.
    seeds |= {m for m, imports in graph.items() if imports & (changed - graph.keys())}
    closure = reverse_closure(graph, seeds)
    if closure >= graph.keys():
        return full("the changed closure is the whole library", reference=reference)
    return {
        "scope": "changed",
        "reference": reference,
        "reference_scope": entry.get("scope"),
        "changed_files": len(changes),
        "changed_modules": len(seeds),
        "modules": sorted(closure),
        "library_modules": len(graph),
    }


def _attribute_change(worktree: Path, reference: str, paths: list[str]) -> str:
    """Why a changed module may set an attribute on a declaration outside the closure, or ``""``."""
    if not paths:
        return ""
    try:
        proc = _git(
            worktree, "diff", "--no-ext-diff", "--no-textconv", "--no-renames", "-U0", reference, "HEAD", "--", *paths
        )
    except (OSError, subprocess.TimeoutExpired):
        return "the changed lines are unavailable"
    if proc.returncode != 0:
        return "the changed lines are unavailable"
    path, header = "", False
    for line in proc.stdout.decode("utf-8", "surrogateescape").splitlines():
        if line.startswith("diff --git "):
            path, header = line.rsplit(" b/", 1)[-1], True
            continue
        if header:
            header = not line.startswith("@@")
            continue
        if line[:1] in "+-" and ATTRIBUTE_LINE.search(line):
            return f"{path} changes a line that may set another module's attribute: {line[:120]}"
    return ""


def write_modules(path: Path, modules: list[str]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(m + "\n" for m in modules))
    return path


def summary(scope: dict) -> dict:
    """``scope`` without the module list, for task records."""
    return {k: v for k, v in scope.items() if k != "modules"} | (
        {"modules": len(scope["modules"])} if "modules" in scope else {}
    )


# ----------------------------------------------------------------------------------------------
# The environment-wide linters over the whole library


class ReportError(ValueError):
    """The driver's output is not a complete, well-formed report: the pass cannot be trusted."""


_HEADER = re.compile(
    r"-- Found (\d+) errors? in \d+ declarations \(plus \d+ automatically generated ones\) "
    r"in TauCeti with (\d+) linters"
)
_SECTION = re.compile(r"^/- The `([^`]*)` linter reports:")


def parse_report(output: str, *, tag: str, marker: str, linters: list[str], rc: int) -> set[tuple[str, str]]:
    """The ``(linter, declaration)`` violations in the pinned driver's report, read with the grammar
    TauCetiTools ``lint-env.sh`` enforces: one header (anchored on the unpredictable ``tag`` when it
    reports errors), the per-run ``marker`` after it, an exit code that agrees with the count, sections
    only for requested linters and at most once each, and one closed ``#check`` block per violation.
    Anything else raises :class:`ReportError`."""
    lines = output.splitlines()
    headers = []
    for index, line in enumerate(lines):
        if line.startswith(tag + ":"):
            rest = re.fullmatch(r"\d+:\d+: error: (.*)", line[len(tag) + 1 :])
            if rest and _HEADER.fullmatch(rest.group(1)):
                headers.append((index, "error", _HEADER.fullmatch(rest.group(1))))
                continue
        if _HEADER.fullmatch(line):
            headers.append((index, "bare", _HEADER.fullmatch(line)))
    if len(headers) != 1:
        raise ReportError(f"{len(headers)} report headers")
    index, kind, header = headers[0]
    count, nlinters = int(header.group(1)), int(header.group(2))
    if nlinters != len(linters):
        raise ReportError(f"the report covers {nlinters} linters, {len(linters)} were requested")
    report = lines[index + 1 :]
    if marker not in (line.strip() for line in report):
        raise ReportError("the driver did not run to completion (no completion marker)")
    if kind == "error" and (rc == 0 or count == 0) or kind == "bare" and (rc != 0 or count != 0):
        raise ReportError(f"exit {rc} disagrees with a {kind} report of {count} violation(s)")
    violations: list[tuple[str, str]] = []
    seen: set[str] = set()
    linter, in_block = None, False
    for line in report:
        if in_block:
            if line.startswith("#check "):
                raise ReportError("a #check block opened before the previous one closed")
            if re.search(r"-/\s*$", line):
                in_block = False
            continue
        section = _SECTION.match(line)
        if section:
            if section.group(1) not in linters or section.group(1) in seen:
                raise ReportError(f"an unexpected section for {section.group(1)!r}")
            seen.add(section.group(1))
            linter = section.group(1)
            continue
        if line.startswith("#check "):
            if linter is None:
                raise ReportError("a #check block before any section")
            fields = line.split()
            violations.append((linter, fields[1].removeprefix("@") if len(fields) > 1 else ""))
            in_block = not re.search(r"-/\s*$", line)
    if in_block:
        raise ReportError("an unterminated #check block")
    if len(violations) != count:
        raise ReportError(f"the header claims {count} violation(s), {len(violations)} were parsed")
    return set(violations)


def locations(output: str, new: set[tuple[str, str]]) -> dict:
    """``LINT-ENV-LOCATIONS``' payload for ``new`` from the driver's optional origin metadata, which only
    ever locates a finding; an unmatched one is kept with an unknown location."""
    origins: dict[tuple[str, str], dict] = {}
    for line in output.splitlines():
        if line.startswith("LINT-ENV-ORIGINS "):
            try:
                records = json.loads(line[len("LINT-ENV-ORIGINS ") :]).get("records") or []
            except (ValueError, AttributeError):
                records = []
            for record in records if isinstance(records, list) else []:
                if isinstance(record, dict) and isinstance(record.get("module"), str):
                    origins.setdefault((str(record.get("linter")), str(record.get("report_name"))), record)
    findings = []
    for linter, name in sorted(new):
        origin = origins.get((linter, name))
        module = origin.get("module", "") if origin else ""
        if module.startswith(ROOT + ".") and isinstance(origin.get("line"), int):
            findings.append(
                {
                    "linter": linter,
                    "declaration": name,
                    "path": module.replace(".", "/") + ".lean",
                    "line": origin["line"],
                    "column": origin.get("column", 0) if isinstance(origin.get("column"), int) else 0,
                }
            )
        else:
            findings.append({"linter": linter, "declaration": name, "unknown": "unlocated"})
    return {"version": 1, "findings": findings}

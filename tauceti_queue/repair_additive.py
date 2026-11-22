"""A mechanical pre-pass for rebase conflicts in which both sides only added lines.

About 30% of the conflicts in large-batch rebases are additive: ``main`` (with the earlier members, the
clean prefix, "ours") and the conflicting member ("theirs") each inserted lines at the same point of a
file they otherwise agree on. Typical cases are a module docstring's "Main results" list, new
declarations next to each other and the import block. Before an agent runs on a conflict,
``repair_task.resolve_one`` calls :func:`resolve`; when every conflicting hunk of every conflicting
file is unambiguously additive and passes the Lean checks below, it writes the union (ours' lines,
then theirs') and the resolution goes through the same checks as an agent's. Anything else returns a
reason and the agent gets the conflict exactly as before.

Each conflicting file is merged again with ``git merge-file --diff3`` (base: the conflicting commit's
parent, ours: the clean prefix, theirs: the commit). A hunk is additive when ours and theirs both keep
the hunk's base lines and only insert lines at one common point of them (an empty base is the usual
case). The insertion point then decides what is allowed:

- the import header: imports only, deduplicated against the whole header, and the header sorted again
  when ours' import block was a sorted contiguous block;
- inside a module docstring (``/-! ... -/``): list items (``*``, ``-``, ``+``, ``1.``) and their
  continuation lines only, no comment delimiters, no item both sides added;
- between top-level commands, outside any comment: each side's lines are whole commands (each command
  starts at column 0 with a command keyword, an attribute or a doc comment; anything indented continues
  it), the next line after the hunk starts a command too, so the hunk is not inside a proof or term,
  and no side ends in an attribute, a doc comment or ``... in`` that would attach to what follows.
  Comments and ``namespace``/``section``/``mutual`` blocks must balance within each side, and no side
  may change the scope that follows it (``open``, ``variable``, ``universe``, ``set_option``, notation,
  ``include``/``omit`` outside a block it closes). A declaration name (with its namespace) declared
  twice in the union file, or a paragraph both sides add, rejects the conflict.

The worker's whole-library verification build stays the safety net: a mechanical resolution can still
put a declaration before what it uses, or interact through ``simp`` sets. The resolution's commit
explanation starts with :data:`MARKER`, so the task record and a later reuse identify it
(:func:`marked`), and findings of the verification build in a mechanically resolved file are reported
as such and repaired by the normal repair round (``repair_task``).
"""

from __future__ import annotations

import dataclasses
import os
import re
import subprocess
import tempfile
from pathlib import Path

MARKER = "Resolved mechanically by the worker (additive union):"
MAX_BYTES = 512_000  # a larger file is left to the agent
LABELS = ("ours", "base", "theirs")
_MARKER_LINE = re.compile(r"^(?:<{7}|\|{7}|={7}|>{7})(?: |$)")

IMPORT = re.compile(r"^(?:(?:public|private|meta)\s+)*import\s+\S+\s*$")
HEADER_LINE = re.compile(r"^(?:module|prelude)\s*$")
LIST_ITEM = re.compile(r"^\s*(?:[*+-]|\d+[.)])\s")

MODIFIERS = r"(?:(?:private|protected|noncomputable|unsafe|partial|nonrec|public|meta|scoped|local)\s+)*"
DECLARATION_KEYWORDS = (
    "theorem",
    "lemma",
    "def",
    "abbrev",
    "instance",
    "structure",
    "class",
    "inductive",
    "opaque",
    "axiom",
    "irreducible_def",
)
COMMAND_KEYWORDS = (
    *DECLARATION_KEYWORDS,
    "example",
    "alias",
    "attribute",
    "open",
    "namespace",
    "section",
    "end",
    "variable",
    "universe",
    "set_option",
    "notation",
    "infix",
    "infixl",
    "infixr",
    "prefix",
    "postfix",
    "macro",
    "macro_rules",
    "syntax",
    "elab",
    "elab_rules",
    "deriving",
    "initialize",
    "mutual",
    "omit",
    "include",
    "export",
    "add_decl_doc",
    "simproc",
    "dsimproc",
    "assert_not_exists",
    "library_note",
)
COMMAND = re.compile(r"^(?:@\[|/--|/-!|/-|--|#[a-z_]+\b|" + MODIFIERS + r"(?:" + "|".join(COMMAND_KEYWORDS) + r")\b)")
DECLARATION = re.compile(
    r"^(?:@\[[^\]]*\]\s*)?" + MODIFIERS + r"(?:class\s+)?(?:" + "|".join(DECLARATION_KEYWORDS) + r")\s+([^\s:({\[⦃]+)"
)
# Column-0 lines that continue the command before them instead of starting one.
CONTINUATION = re.compile(r"^(?:\||where\b|termination_by\b|decreasing_by\b|deriving\b|\)|\]|\})")
# Commands that change how what follows them elaborates, unless they end with `in` (or sit in a block
# that the same side closes).
SCOPE = re.compile(
    r"^(?:open|variable|universe|set_option|include|omit|export|suppress_compilation|unseal|seal|"
    r"(?:local|scoped)\s+\S+|notation|infix|infixl|infixr|prefix|postfix|macro|macro_rules|syntax|elab|elab_rules)\b"
)
OPENER = re.compile(r"^(?:noncomputable\s+)?(?:namespace|section|mutual)\b")
END = re.compile(r"^end\b")
DANGLING = re.compile(
    r"(?:^@\[.*\]$|\bin$|^(?:(?:private|protected|noncomputable|unsafe|partial|nonrec|public|meta)\s*)+$)"
)


@dataclasses.dataclass
class Union:
    files: dict[str, bytes]  # conflicting path -> the resolved file
    hunks: int
    kinds: dict[str, int]  # imports / docstring / declarations -> hunks
    note: str  # the resolution's commit explanation, starting with MARKER


class Refused(Exception):
    """The conflict is not unambiguously additive; the message says why."""


def enabled(environ: dict | None = None) -> bool:
    """``TAUCETI_QUEUE_ADDITIVE_PREPASS``: on unless ``0``, ``off``, ``false`` or ``no``."""
    value = (os.environ if environ is None else environ).get("TAUCETI_QUEUE_ADDITIVE_PREPASS", "")
    return value.strip().lower() not in ("0", "off", "false", "no")


def marked(message: str) -> bool:
    """Whether a commit message carries a mechanical resolution's explanation."""
    return MARKER in message


def resolve(repo: Path, prefix: str, commit: str, paths: list[str]) -> Union | str:
    """The additive union of ``commit``'s conflicting ``paths`` onto ``prefix``, or the reason it does not
    apply. Reads blobs from ``repo``; writes nothing."""
    if not paths:
        return "no conflicting paths"
    files: dict[str, bytes] = {}
    kinds: dict[str, int] = {}
    hunks = 0
    for path in paths:
        try:
            files[path], file_kinds = _resolve_file(repo, prefix, commit, path)
        except Refused as refusal:
            return f"{path}: {refusal}"
        for kind in file_kinds:
            kinds[kind] = kinds.get(kind, 0) + 1
        hunks += len(file_kinds)
    described = ", ".join(f"{n} {kind}" for kind, n in sorted(kinds.items()))
    note = (
        f"{MARKER} both sides only added lines at the same point in {', '.join(f'`{p}`' for p in paths)} "
        f"({hunks} hunk{'s' if hunks != 1 else ''}: {described}); the worker kept the target's lines, "
        "then this PR's."
    )
    return Union(files=files, hunks=hunks, kinds=kinds, note=note)


def _blob(repo: Path, rev: str, path: str) -> bytes | None:
    done = subprocess.run(
        ["git", "-C", str(repo), "cat-file", "blob", f"{rev}:{path}"], capture_output=True, check=False
    )
    return done.stdout if done.returncode == 0 else None


def _resolve_file(repo: Path, prefix: str, commit: str, path: str) -> tuple[bytes, list[str]]:
    if not path.endswith(".lean"):
        raise Refused("not a Lean file")
    blobs = [_blob(repo, rev, path) for rev in (prefix, f"{commit}^", commit)]
    if any(b is None for b in blobs):
        raise Refused("added or deleted on one side")
    if any(len(b) > MAX_BYTES for b in blobs):
        raise Refused("too large")
    try:
        ours, base, theirs = (b.decode() for b in blobs)
    except UnicodeDecodeError:
        raise Refused("not UTF-8") from None
    return merge_text(ours, base, theirs)


def merge_text(ours: str, base: str, theirs: str) -> tuple[bytes, list[str]]:
    """The additive union of a three-way merge of Lean sources, and each hunk's kind; ``Refused``
    otherwise."""
    for text in (ours, base, theirs):
        if any(_MARKER_LINE.match(line) for line in text.splitlines()):
            raise Refused("a side already contains conflict-marker lines")
        if "\r" in text:
            raise Refused("carriage returns")
    segments = _diff3(ours, base, theirs)
    if not any(kind == "hunk" for kind, *_ in segments):
        raise Refused("no conflicting hunk")
    out: list[str] = []
    kinds: list[str] = []
    sort_imports = False
    for index, segment in enumerate(segments):
        if segment[0] == "text":
            out.extend(segment[1])
            continue
        _, o, b, t = segment
        after = [line for s in segments[index + 1 :] for line in s[1]]  # a later hunk as ours has it
        kind, k, x, y = _insertion(out, o, b, t, after)
        out.extend(b[:k])
        if kind == "imports":
            existing = {line.strip() for line in [*out, *b[k:], *after] if IMPORT.match(line)}
            y = [line for line in y if line.strip() not in existing and line.strip() not in {s.strip() for s in x}]
            sort_imports = sort_imports or _sorted_import_block(ours)
        elif kind == "declarations" and x and y and x[-1].strip() and y[0].strip():
            x = [*x, "\n"]  # a blank line between the two sides' commands, as between any two
        x = [line if line.endswith("\n") else line + "\n" for line in x]
        out.extend(x)
        out.extend(y)
        out.extend(b[k:])
        kinds.append(kind)
    if sort_imports:
        out = _sort_import_block(out)
    text = "".join(out)
    if "declarations" in kinds:
        _check_names(text)
    if _comment_depth(text.splitlines(keepends=True)) != 0:
        raise Refused("the union leaves a comment open")
    return text.encode(), kinds


def _diff3(ours: str, base: str, theirs: str) -> list[tuple]:
    """``git merge-file --diff3``'s output as ``("text", lines)`` and ``("hunk", ours, base, theirs)``."""
    with tempfile.TemporaryDirectory() as scratch:
        names = []
        for label, text in zip(LABELS, (ours, base, theirs), strict=True):
            name = Path(scratch) / label
            name.write_text(text)
            names.append(str(name))
        done = subprocess.run(
            ["git", "merge-file", "-p", "--diff3", *(arg for label in LABELS for arg in ("-L", label)), *names],
            capture_output=True,
            check=False,
        )
    if done.returncode < 0 or done.returncode > 127:
        raise Refused(f"git merge-file failed: {done.stderr.decode(errors='replace').strip()}")
    lines = done.stdout.decode().splitlines(keepends=True)
    segments: list[tuple] = []
    text: list[str] = []
    state, sides = None, {"ours": [], "base": [], "theirs": []}
    for line in lines:
        bare = line.rstrip("\n")
        if state is None and bare == "<<<<<<< ours":
            if text:
                segments.append(("text", text))
                text = []
            state, sides = "ours", {"ours": [], "base": [], "theirs": []}
        elif state == "ours" and bare == "||||||| base":
            state = "base"
        elif state == "base" and bare == "=======":
            state = "theirs"
        elif state == "theirs" and bare == ">>>>>>> theirs":
            segments.append(("hunk", sides["ours"], sides["base"], sides["theirs"]))
            state = None
        elif state is None:
            text.append(line)
        else:
            sides[state].append(line)
    if state is not None:
        raise Refused("unparseable merge output")
    if text:
        segments.append(("text", text))
    return segments


def _splits(side: list[str], base: list[str]) -> set[int]:
    """The points ``k`` at which ``side`` is ``base`` with lines inserted: ``base[:k] + X + base[k:]``."""
    if len(side) < len(base):
        return set()
    tail = len(side) - len(base)
    return {k for k in range(len(base) + 1) if side[:k] == base[:k] and side[k + tail :] == base[k:]}


def _insertion(before: list[str], o: list[str], b: list[str], t: list[str], after: list[str]):
    """The hunk's kind, its insertion point ``k`` in ``b`` and the two insertions, or ``Refused``."""
    common = sorted(_splits(o, b) & _splits(t, b))
    if not common:
        raise Refused("not purely additive: a side changes or removes lines of the base")
    unions = {"".join(b[:k] + o[k : len(o) - (len(b) - k)] + t[k : len(t) - (len(b) - k)] + b[k:]) for k in common}
    if len(unions) != 1:
        raise Refused("ambiguous insertion point")
    k = common[0]
    x, y = o[k : len(o) - (len(b) - k)], t[k : len(t) - (len(b) - k)]
    if not any(line.strip() for line in x) or not any(line.strip() for line in y):
        raise Refused("a side adds only blank lines")
    if [line.strip() for line in x] == [line.strip() for line in y]:
        raise Refused("both sides add the same lines")
    prior, rest = [*before, *b[:k]], [*b[k:], *after]
    depth, kind = _comment_state(prior)
    if depth:
        if kind != "module":
            raise Refused("inside a comment or declaration docstring")
        _check_docstring_items(x, y)
        return "docstring", k, x, y
    if _all_imports(x) and _all_imports(y) and _in_header(prior):
        return "imports", k, x, y
    _check_commands(x, y, prior, rest)
    return "declarations", k, x, y


def _scan(line: str, depth: int, kind: str | None) -> tuple[int, str | None]:
    """Lean block-comment nesting after ``line``, and the outermost comment's kind (module / doc / plain)."""
    i, n, in_string = 0, len(line), False
    while i < n:
        two = line[i : i + 2]
        if depth == 0 and in_string:
            if line[i] == "\\":
                i += 2
                continue
            if line[i] == '"':
                in_string = False
            i += 1
            continue
        if depth == 0 and two == "--":
            break
        if depth == 0 and line[i] == '"':
            in_string = True
            i += 1
            continue
        if two == "/-":
            if depth == 0:
                kind = "module" if line[i + 2 : i + 3] == "!" else "doc" if line[i + 2 : i + 3] == "-" else "plain"
            depth += 1
            i += 2
            continue
        if two == "-/" and depth:
            depth -= 1
            i += 2
            if depth == 0:
                kind = None
            continue
        i += 1
    return depth, kind


def _comment_state(lines: list[str]) -> tuple[int, str | None]:
    depth, kind = 0, None
    for line in lines:
        depth, kind = _scan(line, depth, kind)
    return depth, kind


def _comment_depth(lines: list[str]) -> int:
    return _comment_state(lines)[0]


def _check_docstring_items(x: list[str], y: list[str]) -> None:
    for side in (x, y):
        if any("/-" in line or "-/" in line for line in side):
            raise Refused("a docstring insertion contains comment delimiters")
        first = next(line for line in side if line.strip())
        if not LIST_ITEM.match(first):
            raise Refused("a docstring insertion that is not a list item")
        for line in side:
            if line.strip() and not LIST_ITEM.match(line) and not line[:1].isspace():
                raise Refused("a docstring insertion with prose outside list items")
    ours = {line.strip() for line in x if line.strip()}
    if any(line.strip() in ours for line in y if line.strip()):
        raise Refused("both sides add the same docstring line")


def _all_imports(side: list[str]) -> bool:
    return all(IMPORT.match(line) or not line.strip() for line in side)


def _in_header(prior: list[str]) -> bool:
    """Only the header (``module``, ``prelude``, imports and comments) comes before the insertion point."""
    depth, kind = 0, None
    for line in prior:
        was = depth
        depth, kind = _scan(line, depth, kind)
        if was or depth or not line.strip() or line.lstrip().startswith("--") or line.lstrip().startswith("/-"):
            continue
        if not (IMPORT.match(line) or HEADER_LINE.match(line)):
            return False
    return True


def _import_block(lines: list[str]) -> tuple[int, int] | None:
    """The contiguous import lines of the header, as ``[start, end)``, or None."""
    indices = [i for i, line in enumerate(lines) if IMPORT.match(line)]
    if not indices:
        return None
    start, end = indices[0], indices[-1] + 1
    if end - start != len(indices) or not _in_header(lines[:start]):
        return None
    return start, end


def _sorted_import_block(ours: str) -> bool:
    lines = ours.splitlines(keepends=True)
    block = _import_block(lines)
    if block is None:
        return False
    imports = [line.strip() for line in lines[block[0] : block[1]]]
    return len(imports) > 1 and imports == sorted(imports)


def _sort_import_block(lines: list[str]) -> list[str]:
    block = _import_block(lines)
    if block is None:
        raise Refused("the union's imports are no longer one block")
    start, end = block
    imports = sorted({line.strip() for line in lines[start:end]})
    return [*lines[:start], *(line + "\n" for line in imports), *lines[end:]]


def _check_commands(x: list[str], y: list[str], prior: list[str], rest: list[str]) -> None:
    following = next((line for line in rest if line.strip()), None)
    if following is not None and not (COMMAND.match(following) and not following[:1].isspace()):
        raise Refused("the insertion point is inside a declaration, proof or term")
    previous = _last_code_line(prior)
    if previous is not None and DANGLING.search(previous.strip()):
        raise Refused("the insertion point follows an attribute, modifier or `in`")
    if _ends_in_docstring(prior):
        raise Refused("the insertion point follows a doc comment")
    for side in (x, y):
        _check_side(side)
    paragraphs = {p for p in _paragraphs(x)}
    if any(p in paragraphs for p in _paragraphs(y)):
        raise Refused("both sides add the same paragraph")
    elsewhere = set(_paragraphs([*prior, *rest]))
    if any(p in elsewhere for p in [*_paragraphs(x), *_paragraphs(y)] if "\n" in p):
        raise Refused("a side adds a paragraph the file already has")


def _last_code_line(lines: list[str]) -> str | None:
    depth, kind = 0, None
    last = None
    for line in lines:
        was = depth
        depth, kind = _scan(line, depth, kind)
        if not was and not depth and line.strip() and not line.lstrip().startswith("--"):
            last = line
    return last


def _ends_in_docstring(lines: list[str]) -> bool:
    """The last non-blank line closes a ``/-- ... -/`` doc comment."""
    depth, kind, closed = 0, None, None
    for line in lines:
        if not line.strip():
            continue
        opened = kind
        depth, kind = _scan(line, depth, kind)
        closed = (kind or opened) if depth == 0 and line.rstrip().endswith("-/") else None
    return closed == "doc"


def _check_side(side: list[str]) -> None:
    depth, kind = 0, None
    blocks = 0
    first = True
    last_code = None
    for line in side:
        was = depth
        depth, kind = _scan(line, depth, kind)
        if was or not line.strip() or line[:1].isspace():
            if was == 0 and line.strip() and first:
                raise Refused("an insertion starting inside a command")
            continue
        if first:
            if not COMMAND.match(line) or END.match(line):
                raise Refused("an insertion that does not start a command")
            first = False
        elif not COMMAND.match(line) and not CONTINUATION.match(line):
            raise Refused(f"an unrecognised top-level line: {line.strip()[:60]}")
        if not line.startswith(("--", "/-")):
            last_code = line
        if OPENER.match(line):
            blocks += 1
        elif END.match(line):
            blocks -= 1
            if blocks < 0:
                raise Refused("an insertion closes a block it did not open")
        elif blocks == 0 and SCOPE.match(line) and not re.search(r"\bin\s*$", line):
            raise Refused(f"an insertion changes the scope after it: {line.strip()[:60]}")
    if depth:
        raise Refused("an insertion leaves a comment open")
    if blocks:
        raise Refused("an insertion leaves a namespace or section open")
    if last_code is not None and DANGLING.search(last_code.strip()):
        raise Refused("an insertion ends in an attribute, modifier or `in`")
    if _ends_in_docstring(side):
        raise Refused("an insertion ends in a doc comment")


def _paragraphs(lines: list[str]) -> list[str]:
    out, current = [], []
    for line in [*lines, "\n"]:
        if line.strip():
            current.append(line.strip())
        elif current:
            out.append("\n".join(current))
            current = []
    return out


def _check_names(text: str) -> None:
    """No declaration name, qualified by its namespaces, is declared twice in ``text``."""
    seen: set[str] = set()
    stack: list[str] = []
    depth, kind = 0, None
    for line in text.splitlines(keepends=True):
        was = depth
        depth, kind = _scan(line, depth, kind)
        if was or line[:1].isspace():
            continue
        stripped = line.strip()
        opened = re.match(r"^(?:noncomputable\s+)?(namespace|section|mutual)\b\s*(\S*)", stripped)
        if opened:
            stack.append(opened.group(2) if opened.group(1) == "namespace" else "")
            continue
        if END.match(stripped):
            if stack:
                stack.pop()
            continue
        declared = DECLARATION.match(stripped)
        if not declared:
            continue
        name = declared.group(1)
        if name.startswith("_root_."):
            full = name[len("_root_.") :]
        else:
            full = ".".join([s for s in stack if s] + [name])
        if full in seen:
            raise Refused(f"the union declares `{full}` twice")
        seen.add(full)

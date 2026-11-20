"""A mechanical pre-pass for rebase conflicts caused by renamed numeric projections.

When ``main`` rewrites numeric projections (``x.1`` to ``x.fst``, TauCetiTools ``tauceti-tools
numproj``), a member written against the old ``main`` conflicts wherever its change touches a line
``main`` rewrote. Before the agent runs on such a conflict, ``resolve_conflicts`` calls
:func:`prepass`: for each conflicting Lean file it rewrites the conflicting commit's parent P and the
commit C with the same tool, giving P' and C', and merges with ``git merge-file`` with base P', ours
the clean prefix (already rewritten on the new ``main``) and theirs C'. The tool elaborates each file
against the worktree's build, so it runs in the build sandbox like every other build of member code,
on copies of the two versions under ``.lake``; the worktree's sources are not touched until the merges
are done. A rebase task's tree is not built when it is prepared (its artifacts are only in Lake's
artifact cache), so the project modules the two versions import are built first (``lake build``,
under the worktree's build lock), mostly restored from that cache.

The result is mechanical only when every conflicting file merged cleanly and the tool rewrote C's
versions completely (exit 0). Otherwise the merged files, clean or with conflict markers, are the
agent's starting point and :attr:`Prepass.note` tells it what the worker did. Anything unexpected
(the tool missing or failing, a rewrite that changes more than projections and layout, nothing
rewritten) returns None and leaves the worktree as ``apply_conflict`` wrote it.

The same tool serves repair and preparation tasks (``rewrite_chain``): before their agent runs, the
files with a "Numeric projections are disallowed" finding are rewritten in every version along the
chain and the chain recommitted, so each member's commit carries the rewritten form of its own
change, and the agent gets only what a scoped build of those files still reports.
"""

from __future__ import annotations

import bisect
import dataclasses
import hashlib
import importlib.util
import json
import os
import re
import secrets
import shutil
import stat
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from tauceti_queue import repair_sandbox

TOOL_FILES = ("numproj_fix.py", "NumProjScan.lean", "local_axioms.py")
SCRATCH = "numproj"  # under the worktree's `.lake`, the only part of the tree the sandbox can write
TIMEOUT_SECONDS = 1800.0
# The host-side check of a rewrite is near-linear but not strictly so (the tool's lexer and line
# copies); a larger Lean file is left to the agent. TauCeti's largest sources are well below this.
MAX_BYTES = 256_000
LOG_BYTES = 64_000  # of a tool log, for the sites it reports to the agent
JOBS = 8
# A numeric projection as written: digits after a `.` that follows an identifier or a closing bracket.
# A file without one has nothing for the tool to rewrite.
# The words of a Lean header besides `import` and the module names (`header_imports`).
HEADER_WORDS = ("module", "prelude", "public", "private", "meta", "all")
CANDIDATE = re.compile(r"[\w'!?\)\]⟩»·℘]\.\d")
# For `projection_edits`: a numeric projection, the named field replacing it, and a receiver the
# tool may parenthesize.
_NUMERIC = re.compile(r"\.\d+")
_NAMED = re.compile(r"\.[^\W\d][\w'!?]*")
_RECEIVER = re.compile(r"[\w'.$!?«»℘]+")
_BEFORE_PROJECTION = ")]⟩»·℘"
_RECEIVER_BOUNDARY = " \t\n([{⟨,;"


@dataclasses.dataclass
class Prepass:
    clean: bool  # every conflicting file merged cleanly and C was rewritten completely
    merged: list[str]  # conflicting paths that merged cleanly after the rewrite
    remaining: list[str]  # conflicting paths that still conflict (or were not rewritten)
    note: str  # for the agent (or the commit's explanation when ``clean``)
    tool: dict  # the two tool runs: rc, seconds, log


def tool_dir() -> Path | None:
    """The installed TauCetiTools' numproj scripts, or None when this TauCetiTools has none."""
    try:
        from tauceti_tools.templates import ASSETS
    except Exception:  # noqa: BLE001 - an older or broken TauCetiTools has no numproj
        return None
    scripts = Path(ASSETS) / "scripts"
    return scripts if all((scripts / name).is_file() for name in TOOL_FILES) else None


def _blob(worktree: Path, rev: str, path: str, limit: int | None = MAX_BYTES) -> bytes | None:
    """``path`` at ``rev`` when it is a regular file there (not a symlink or submodule) of at most
    ``limit`` bytes, else None."""
    listing = subprocess.run(
        ["git", "-C", str(worktree), "ls-tree", "-l", "-z", rev, "--", path],
        capture_output=True,
        text=True,
        check=False,
    ).stdout.split("\0")[0]
    head, _, listed = listing.partition("\t")
    fields = head.split()  # mode, type, object, size
    if listed != path or len(fields) != 4 or fields[0] not in ("100644", "100755") or not fields[3].isdigit():
        return None
    if limit is not None and int(fields[3]) > limit:
        return None
    proc = subprocess.run(
        ["git", "-C", str(worktree), "cat-file", "blob", f"{rev}:{path}"], capture_output=True, check=False
    )
    return proc.stdout if proc.returncode == 0 else None


def _write(path: Path, data: bytes) -> None:
    """Replace the worktree's regular file ``path``, never writing through a symlink."""
    fd = os.open(path, os.O_WRONLY | os.O_TRUNC | os.O_NOFOLLOW)
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)


def projection_edits(before: str, after: str) -> set[tuple[int, int, str, tuple[int, int] | None]] | None:
    """The numproj findings that would turn ``before`` into ``after``, in ``numproj_fix.apply``'s
    form ``(line, col, field, span)``: a numeric projection's digits after an identifier or bracket
    replaced by a field name, with the receiver from ``span[0]`` to the `.` at ``span[1]``
    parenthesized when a `)` is inserted right before it; spaces may become line breaks with
    indentation (``apply`` rewraps). None when ``after`` has any other difference. ``rewrites_only``
    then checks the findings by applying them with the tool's own code."""
    edits: set[tuple[int, int, str, tuple[int, int] | None]] = set()

    starts = [0] + [k + 1 for k, ch in enumerate(before) if ch == "\n"]

    def place(i: int) -> tuple[int, int]:
        line = bisect.bisect_right(starts, i)
        return line, i - starts[line - 1]

    i = j = 0
    opened: int | None = None  # where in ``before`` an inserted `(` stands
    span: tuple[int, int] | None = None  # a receiver closed by an inserted `)`, for the next projection
    while i < len(before) or j < len(after):
        numeric = _NUMERIC.match(before, i)
        named = _NAMED.match(after, j)
        if numeric and named and j > 0:
            # A projection follows a bracket or an identifier, as rewritten so far (`x.1.2` is
            # `x.fst.2` by now); digits alone before the `.` are a literal (`1.5`), unless they are
            # themselves a projection the tool kept (`x.1.2` to `x.1.snd`).
            k = j  # back over the identifier just before the `.`, if any (linear: no regex search)
            while k > 0 and (after[k - 1].isalnum() or after[k - 1] in "_'!?"):
                k -= 1
            ident = after[k:j]
            if after[j - 1] not in _BEFORE_PROJECTION and (not ident or (ident.isdigit() and after[k - 1 : k] != ".")):
                return None
            line, col = place(i)
            edits.add((line, col + 1, named.group()[1:], span))
            i, j, span = numeric.end(), named.end(), None
        elif span is not None:
            return None  # an inserted `)` belongs to the projection right after it
        elif i < len(before) and j < len(after) and before[i] == after[j]:
            i, j = i + 1, j + 1
        elif j < len(after) and after[j] == "(" and opened is None:
            opened, j = i, j + 1
        elif j < len(after) and after[j] == ")" and opened is not None and _NUMERIC.match(before, i):
            # An identifier chain, `$h` or `℘s`: never digits first, which would be a literal (`1.5`).
            receiver = _RECEIVER.fullmatch(before, opened, i)
            # The whole receiver: `returnh.1` may not become `return(h).fst`. Lean's identifier
            # characters are many, so only an explicit boundary before the receiver counts.
            joined = opened > 0 and before[opened - 1] not in _RECEIVER_BOUNDARY
            if not receiver or joined or before[opened].isdigit() or place(opened)[0] != place(i)[0]:
                return None
            span = (place(opened)[1], place(i)[1])
            opened, j = None, j + 1
        elif i < len(before) and before[i] == " " and after[j : j + 1] == "\n":
            i, j = i + 1, j + 1
            while j < len(after) and after[j] == " ":
                j += 1
        else:
            return None
    return edits if opened is None and span is None else None


def load_tool(tool: Path):
    """The installed ``numproj_fix.py`` as a module, for its ``apply`` and lexer (stdlib only; its
    command line is behind ``__main__``). This is the installed copy, never the sandbox's."""
    path = tool / "numproj_fix.py"
    name = "tauceti_numproj_fix_" + hashlib.sha256(path.read_bytes()).hexdigest()[:16]
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def rewrites_only(before: str, after: str, fix) -> bool:
    """Whether ``after`` is exactly what the numproj tool ``fix`` (``load_tool``) writes for
    ``before`` given some findings: numeric projections in code (by the tool's own lexer, so not in
    comments or string literals) named, receivers of identifier characters parenthesized, and the
    tool's own rewrapping. The elaborated member code can write the tool's files, so this bounds what
    the sandbox can change: which field a numeric projection gets, which the build checks."""
    edits = projection_edits(before, after)
    if not edits:
        return before == after
    lines = before.split("\n")
    lexes = fix.line_lexes(lines)
    for line, col, _, span in edits:
        kinds = lexes[line - 1][0]
        if kinds[col] != fix.CODE or (span is not None and kinds[span[0]] != fix.CODE):
            return False
    with tempfile.TemporaryDirectory(prefix="numproj-check-") as scratch:
        path = Path(scratch) / "file.lean"
        path.write_text(before, encoding="utf-8")
        fix.apply({str(path): edits})
        return path.read_text(encoding="utf-8") == after


def _private_dir(lake: Path, *names: str) -> Path:
    """``lake/names...``, created 0700 where missing. Elaborated member code can write under
    ``.lake``, so no component may be a symlink: an error rather than following one out of the tree."""
    path = lake
    for name in ("", *names):
        path = path / name if name else path
        try:
            os.mkdir(path, 0o700)
        except FileExistsError:
            pass
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            if name:
                os.fchmod(fd, 0o700)
        finally:
            os.close(fd)
    return path


def _read(directory: int, name: str) -> bytes:
    """The regular file ``name`` in the open ``directory``, never through a symlink and never
    blocking on a FIFO: elaborated member code may have replaced it."""
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    with os.fdopen(fd, "rb") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise OSError(f"{name} is not a regular file")
        return handle.read(2 * MAX_BYTES + 1)


def _run_tool(worktree: Path, env: dict, tool: Path, files: list[Path], cache: Path, log: Path, timeout: float) -> dict:
    from tauceti_queue import repair

    argv = ["python3", "-I", str(tool / "numproj_fix.py"), "--jobs", str(JOBS), *[str(f) for f in files]]
    extra = {"TAUCETI_NUMPROJ_CACHE": str(cache)}
    run_env = {**env, **extra}
    try:
        wrapped = repair_sandbox.sandboxed_lake(argv, worktree, run_env, extra_env=extra)
    except repair_sandbox.SandboxUnavailable as error:
        return {"rc": 127, "error": str(error), "log": str(log)}
    return repair.run_logged(wrapped, worktree, log, env=run_env, timeout=timeout)


def _without_comments(text: str) -> str:
    """``text`` with Lean's line comments and (nested) block comments replaced by spaces."""
    out, depth, i = [], 0, 0
    while i < len(text):
        two = text[i : i + 2]
        if depth == 0 and two == "--":
            end = text.find("\n", i)
            i = len(text) if end < 0 else end
        elif two == "/-":
            depth, i = depth + 1, i + 2
        elif depth and two == "-/":
            depth, i = depth - 1, i + 2
            out.append(" ")
        else:
            if depth == 0:
                out.append(text[i])
            i += 1
    return "".join(out)


def header_imports(text: str) -> list[str]:
    """The modules a Lean file's header imports: its leading `module`, `prelude` and `import` lines with
    their modifiers, comments skipped, up to the first other token."""
    modules, expecting = [], False
    for token in _without_comments(text).split():
        if expecting:
            if token != "all":
                modules.append(token)
                expecting = False
        elif token == "import":
            expecting = True
        elif token not in HEADER_WORDS:
            break
    return modules


def imported_modules(worktree: Path, sides: dict) -> list[str]:
    """The project modules the two versions of the conflicting files import: those with a source in
    the worktree."""
    modules = set()
    for _, base, theirs in sides.values():
        for text in (base, theirs):
            modules.update(header_imports(text.decode(errors="replace")))
    return sorted(m for m in modules if (worktree / module_path(m)).is_file())


def module_path(module: str) -> str:
    return module.replace(".", "/") + ".lean"


def _build_imports(
    worktree: Path,
    prefix: str,
    commit: str,
    sides: dict,
    paths: list[str],
    env: dict,
    log: Path,
    timeout: float,
    lake,
) -> bool:
    """Build the modules the tool needs to elaborate the files (``imported_modules``), with the clean
    prefix's version of every conflicting file in place of git's markers meanwhile (one of the modules
    may import one); the worktree is then back at ``repair.apply_conflict``'s state. False when the
    build fails, since the tool cannot elaborate the files then."""
    from tauceti_queue import repair, repair_tools

    modules = imported_modules(worktree, sides)
    if not modules:
        return True
    with repair_tools.build_lock(worktree, log=lambda *a, **k: None):
        try:
            for path in paths:  # rewritten or not, any of them may be imported
                clean = _blob(worktree, prefix, path, limit=None)
                if clean is not None and (worktree / path).is_file() and not (worktree / path).is_symlink():
                    _write(worktree / path, clean)
            return _lake_build(worktree, modules, env, log, timeout, lake)["rc"] == 0
        finally:
            repair.apply_conflict(worktree, prefix, commit)


def _lake_build(worktree: Path, modules: list[str], env: dict, log: Path, timeout: float, lake) -> dict:
    """``lake build`` of ``modules`` in the build sandbox; the caller holds the worktree's build lock."""
    from tauceti_queue import repair

    argv = repair_sandbox.sandboxed_lake([*lake, "build", *modules], worktree, env)
    return repair.run_logged(argv, worktree, log, env=env, timeout=timeout)


def _merge(ours: bytes, base: bytes, theirs: bytes, labels: tuple[str, str, str]) -> tuple[int, bytes] | None:
    """``git merge-file -p``: (conflict count, merged bytes), or None when git fails."""
    with tempfile.TemporaryDirectory(prefix="numproj-merge-") as scratch:
        names = []
        for name, data in (("ours", ours), ("base", base), ("theirs", theirs)):
            path = Path(scratch) / name
            path.write_bytes(data)
            names.append(str(path))
        proc = subprocess.run(
            ["git", "merge-file", "-p", "-L", labels[0], "-L", labels[1], "-L", labels[2], *names],
            capture_output=True,
            check=False,
        )
    if proc.returncode < 0 or proc.returncode > 127:
        return None
    return proc.returncode, proc.stdout


@dataclasses.dataclass
class Rewrite:
    outputs: dict[str, list[bytes]]  # per group, the rewritten blobs in the order given
    runs: dict[str, dict]  # per group, its tool run: rc, seconds, timed_out, log
    copies: dict[str, list[str]]  # per group, the paths the tool was given (its log names them)


def rewrite_blobs(
    worktree: Path,
    env: dict,
    tool: Path,
    fix,
    groups: dict[str, list[bytes]],
    logs: dict[str, Path],
    timeout: float,
) -> Rewrite | None:
    """Rewrite the numeric projections in each group's blobs with the tool, one sandboxed run per
    group, concurrently, on copies under the worktree's ``.lake``. None when a run fails (anything
    but 0, everything rewritten, or 1, some sites left) or a rewrite changes more than projections
    and layout (``rewrites_only``)."""
    scratch, directory = None, None
    try:
        cache = _private_dir(worktree / ".lake", SCRATCH, "cache")  # the compiled scanner, per toolchain
        scratch = _private_dir(worktree / ".lake", SCRATCH, f"run-{secrets.token_hex(6)}")
        copy = scratch / "tool"
        copy.mkdir()
        for name in TOOL_FILES:
            shutil.copyfile(tool / name, copy / name)
        # Flat names in a directory held open: the reads after the runs go through no path the
        # elaborated code could have swapped for a symlink. The tool takes any path; it names no module.
        directory = os.open(scratch, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        files: dict[str, list[Path]] = {}
        for group, blobs in groups.items():
            files[group] = [scratch / f"{group}-{k}.lean" for k in range(len(blobs))]
            for target, data in zip(files[group], blobs, strict=True):
                target.write_bytes(data)

        def run(group: str) -> dict:
            return _run_tool(worktree, env, copy, files[group], cache, logs[group], timeout)

        with ThreadPoolExecutor(max_workers=len(groups)) as pool:  # each group elaborates its own files
            runs = dict(zip(groups, pool.map(run, groups), strict=True))
        # 0: everything rewritten; 1: some sites left for the author. Anything else is a tool failure.
        if any(runs[group].get("rc") not in (0, 1) for group in groups):
            return None
        outputs: dict[str, list[bytes]] = {}
        for group, blobs in groups.items():
            outputs[group] = []
            for target, before in zip(files[group], blobs, strict=True):
                after = _read(directory, target.name)
                if len(after) > 2 * MAX_BYTES or not rewrites_only(
                    before.decode(), after.decode(errors="replace"), fix
                ):
                    return None
                outputs[group].append(after)
        return Rewrite(
            outputs=outputs,
            runs={g: {k: runs[g].get(k) for k in ("rc", "seconds", "timed_out", "log")} for g in runs},
            copies={g: [str(f) for f in files[g]] for g in files},
        )
    except (OSError, UnicodeDecodeError, RuntimeError, ValueError):
        return None
    finally:
        if directory is not None:
            os.close(directory)
        if scratch is not None:
            shutil.rmtree(scratch, ignore_errors=True)


def prepass(
    worktree: Path,
    prefix: str,
    commit: str,
    paths: list[str],
    env: dict,
    logdir: Path,
    *,
    timeout: float = TIMEOUT_SECONDS,
    lake: tuple[str, ...] = ("lake",),
) -> Prepass | None:
    """Rewrite numeric projections in both sides of ``commit``'s conflicting Lean files and merge them
    onto the clean ``prefix``, writing the merged files into ``worktree`` (checked out by
    ``repair.apply_conflict``). None when the pre-pass does not apply or anything unexpected happens;
    the worktree then holds ``apply_conflict``'s result as before."""
    tool = tool_dir()
    if tool is None:
        return None
    try:
        fix = load_tool(tool)
    except Exception:  # noqa: BLE001 - a tool that does not load is a tool that is unavailable
        return None
    parent = subprocess.run(
        ["git", "-C", str(worktree), "rev-parse", "--verify", "--quiet", f"{commit}^"],
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()
    if not parent:
        return None
    sides: dict[str, tuple[bytes, bytes, bytes]] = {}  # path -> (ours, P, C)
    for path in paths:
        if not path.endswith(".lean"):
            continue
        ours, base, theirs = (_blob(worktree, rev, path) for rev in (prefix, parent, commit))
        if ours is None or base is None or theirs is None:
            continue  # an add/delete conflict is not about projections
        try:
            texts = (base.decode(), theirs.decode())
        except UnicodeDecodeError:
            continue
        if any(CANDIDATE.search(t) for t in texts):
            sides[path] = (ours, base, theirs)
    if not sides:
        return None
    try:
        log = logdir / f"numproj-{commit[:12]}-imports.log"
        if not _build_imports(worktree, prefix, commit, sides, paths, env, log, timeout, lake):
            return None
        groups = {"parent": [base for _, base, _ in sides.values()], "commit": [c for _, _, c in sides.values()]}
        logs = {side: logdir / f"numproj-{commit[:12]}-{side}.log" for side in groups}
        rewrite = rewrite_blobs(worktree, env, tool, fix, groups, logs, timeout)
        if rewrite is None:
            return None
        runs = rewrite.runs
        rewritten = dict(
            zip(sides, zip(rewrite.outputs["parent"], rewrite.outputs["commit"], strict=True), strict=True)
        )
        if all(rewritten[path] == (base, theirs) for path, (_, base, theirs) in sides.items()):
            return None
        labels = (prefix[:12], f"{parent[:12]} (projections rewritten)", f"{commit[:12]} (projections rewritten)")
        merged, remaining, outputs = [], [], {}
        for path, (new_base, new_theirs) in rewritten.items():
            result = _merge(sides[path][0], new_base, new_theirs, labels)
            if result is None:
                return None
            conflicts, outputs[path] = result
            (remaining if conflicts else merged).append(path)
        untouched = [p for p in paths if p not in sides]
        complete = runs["commit"]["rc"] == 0
        clean = complete and not remaining and not untouched
        outcome = Prepass(
            clean=clean,
            merged=merged,
            remaining=remaining + untouched,
            note=_note(
                clean,
                complete,
                merged,
                remaining,
                untouched,
                runs,
                dict(zip(rewrite.copies["commit"], sides, strict=True)),
            ),
            tool=runs,
        )
    except (OSError, UnicodeDecodeError, RuntimeError, ValueError):
        return None
    try:
        for path, data in outputs.items():
            _write(worktree / path, data)
    except OSError:
        from tauceti_queue import repair

        repair.apply_conflict(worktree, prefix, commit)  # back to git's own conflict, as without the pre-pass
        return None
    return outcome


# ----------------------------------------------------------------------------------------------
# The findings of a repair: numeric projections along the chain


LINT = "Numeric projections are disallowed"  # the rc1 linter's message (`linter.style.numericProjections`)
EXPLANATION = "Numeric projections rewritten mechanically by the repair worker (tauceti-tools numproj)."


@dataclasses.dataclass
class ChainRewrite:
    tip: str  # the rewritten chain, checked out in the worktree
    paths: list[str]  # the files with rewritten versions
    edited: list[str]  # the original commits whose own change was rewritten (their messages say so)
    errors: list[dict]  # the scoped build's errors in ``paths`` (``repair_stream.parse_errors``)
    tool: dict


def _listing(worktree: Path, rev: str, paths: list[str]) -> dict[str, tuple[str, str]]:
    """``path`` to ``(mode, blob id)`` at ``rev`` for those of ``paths`` that are regular files there."""
    out = subprocess.run(
        ["git", "-C", str(worktree), "ls-tree", "-z", rev, "--", *paths], capture_output=True, text=True, check=True
    ).stdout
    entries = {}
    for row in out.split("\0"):
        head, _, path = row.partition("\t")
        fields = head.split()
        if path in paths and len(fields) == 3 and fields[0] in ("100644", "100755") and fields[1] == "blob":
            entries[path] = (fields[0], fields[2])
    return entries


def _git_bytes(worktree: Path, *args: str, data: bytes | None = None, env: dict | None = None) -> str:
    return subprocess.run(
        ["git", "-C", str(worktree), *args], input=data, capture_output=True, check=True, env=env
    ).stdout.decode()


def _with_entries(worktree: Path, tree: str, entries: dict[str, tuple[str, str]]) -> str:
    """``tree`` with each path's entry replaced by ``(mode, blob id)``."""
    with tempfile.TemporaryDirectory(prefix="numproj-index-") as scratch:
        env = {**os.environ, "GIT_INDEX_FILE": str(Path(scratch) / "index")}
        _git_bytes(worktree, "read-tree", tree, env=env)
        rows = "".join(f"{mode} {blob}\t{path}\0" for path, (mode, blob) in entries.items())
        _git_bytes(worktree, "update-index", "-z", "--index-info", data=rows.encode(), env=env)
        return _git_bytes(worktree, "write-tree", env=env).strip()


def _patch(worktree: Path, old: tuple[str, str] | None, new: tuple[str, str] | None) -> list[str]:
    """The lines a change from blob ``old`` to blob ``new`` removes and adds (None: no file)."""
    if old is None or new is None:  # git diffs two objects; the empty blob stands for a missing file
        empty = _git_bytes(worktree, "hash-object", "-w", "--stdin", data=b"").strip()
        old, new = old or ("", empty), new or ("", empty)
    diff = _git_bytes(worktree, "diff", "-U0", old[1], new[1])
    return [line for line in diff.splitlines() if line[:1] in ("+", "-") and line[:3] not in ("+++", "---")]


def rewrite_chain(
    worktree: Path,
    base: str,
    paths: list[str],
    env: dict,
    logdir: Path,
    *,
    frozen: frozenset[str] = frozenset(),
    timeout: float = TIMEOUT_SECONDS,
    lake: tuple[str, ...] = ("lake",),
) -> ChainRewrite | None:
    """Rewrite the numeric projections in every version of ``paths`` along the chain ``base..HEAD``
    of ``worktree`` and recommit the chain, so that each member's own change becomes the rewritten
    form of what it changed. Every distinct version (blob) is rewritten once, in one tool run; each
    commit's tree gets the rewritten blob of each of its versions, and a commit whose own patch
    changes gets ``EXPLANATION`` appended to its message, keeping its author and date. A file stays
    as at ``base`` until a member changes it: that member takes the rewrite of what it inherits. A file that a ``frozen`` commit (a member never edited) changes is not rewritten.

    The worktree is then checked out at the new tip and the rewritten modules built (``lake build``
    under the build lock); ``errors`` are that build's errors in them. None when nothing is rewritten
    or anything fails (the tool, a rewrite that is not projections only, the build without a single
    error to show for its failure); the worktree is then at its original ``HEAD``."""
    from tauceti_queue import chain as chain_tool
    from tauceti_queue import repair, repair_stream, repair_tools
    from tauceti_queue.diagnostics import module_of

    tool = tool_dir()
    paths = [p for p in dict.fromkeys(paths) if p.endswith(".lean")]
    if tool is None or not paths:
        return None
    try:
        fix = load_tool(tool)
    except Exception:  # noqa: BLE001 - a tool that does not load is a tool that is unavailable
        return None
    try:
        head = chain_tool.resolve_commit(worktree, "HEAD")
        commits = chain_tool.chain_commits(worktree, base, head)
        listings = {sha: _listing(worktree, sha, paths) for sha in (base, *(c.sha for c in commits))}
    except (chain_tool.ChainError, subprocess.CalledProcessError):
        return None
    # A file a frozen member changes is left to the agent: rewriting the version before it would edit
    # that member's change.
    previous = listings[base]
    for c in commits:
        if c.sha in frozen:
            paths = [p for p in paths if listings[c.sha].get(p) == previous.get(p)]
        previous = listings[c.sha]
    # The versions to rewrite: a file's from the first commit that changes it on (before that it is
    # the base's, which stays as it is).
    touched: dict[str, set[str]] = {}  # commit -> the paths changed by it or an earlier commit
    seen: set[str] = set()
    previous = listings[base]
    for c in commits:
        seen.update(p for p in paths if listings[c.sha].get(p) != previous.get(p))
        touched[c.sha], previous = set(seen), listings[c.sha]
    versions: dict[str, bytes] = {}  # blob id -> its content
    for c in commits:
        for path in touched[c.sha]:
            entry = listings[c.sha].get(path)
            if entry is None or entry[1] in versions:
                continue
            data = _blob(worktree, c.sha, path)
            try:
                if data is not None and CANDIDATE.search(data.decode()):
                    versions[entry[1]] = data
            except UnicodeDecodeError:
                continue
    if not versions:
        return None
    modules = {m for data in versions.values() for m in header_imports(data.decode())}
    targets = {module_of(p) for p in paths}
    imports = sorted(m for m in modules - targets if (worktree / module_path(m)).is_file())
    stamp = head[:12]
    try:
        if imports:
            with repair_tools.build_lock(worktree, log=lambda *a, **k: None):
                # A failure leaves the files importing the failed module incomplete; the tool says so.
                _lake_build(worktree, imports, env, logdir / f"numproj-chain-{stamp}-imports.log", timeout, lake)
    except OSError:
        return None
    blobs = list(versions)
    rewrite = rewrite_blobs(
        worktree,
        env,
        tool,
        fix,
        {"chain": [versions[b] for b in blobs]},
        {"chain": logdir / f"numproj-chain-{stamp}.log"},
        timeout,
    )
    if rewrite is None:
        return None
    try:
        renamed = {
            blob: _git_bytes(worktree, "hash-object", "-w", "--stdin", data=after).strip()
            for blob, after in zip(blobs, rewrite.outputs["chain"], strict=True)
            if after != versions[blob]
        }
        if not renamed:
            return None
        parent, edited, changed = chain_tool.resolve_commit(worktree, base), [], set()
        before = {p: listings[base].get(p) for p in paths}  # the rewritten versions of the parent
        previous = listings[base]
        for c in commits:
            listing = listings[c.sha]
            after = {
                p: (listing[p][0], renamed.get(listing[p][1], listing[p][1])) if p in touched[c.sha] else listing[p]
                for p in paths
                if p in listing
            }
            entries = {p: entry for p, entry in after.items() if entry != listing[p]}
            changed.update(entries)
            # Its own change is rewritten when its patch of a file differs from what it was.
            own = any(
                _patch(worktree, previous.get(p), listing.get(p)) != _patch(worktree, before.get(p), after.get(p))
                for p in paths
                if previous.get(p) != listing.get(p) and (p in entries or before.get(p) != previous.get(p))
            )
            before, previous = after, listing
            if not entries and c.parents == (parent,):
                parent = c.sha
                continue
            tree = _with_entries(worktree, c.tree, entries) if entries else c.tree
            message = c.message
            if own:
                edited.append(c.sha)
                if EXPLANATION not in message:
                    message = message.rstrip() + "\n\n" + EXPLANATION + "\n"
            parent = chain_tool.commit_tree(
                worktree,
                tree,
                parent,
                message,
                (c.author_name, c.author_email),
                author_date=c.author_date,
                committer_date=c.author_date,
            )
        repair.git(worktree, "checkout", "--quiet", "--force", "--detach", parent)
    except (chain_tool.ChainError, subprocess.CalledProcessError, RuntimeError, UnicodeDecodeError):
        return None
    rewritten = sorted(changed)
    log = logdir / f"numproj-chain-{parent[:12]}-build.log"
    try:
        with repair_tools.build_lock(worktree, log=lambda *a, **k: None):
            build = _lake_build(worktree, [module_of(p) for p in rewritten], env, log, timeout, lake)
        with log.open("rb") as handle:  # this pass's own log: one build per tip
            text = handle.read().decode(errors="replace")
    except OSError:
        build, text = {"rc": None}, ""
    errors = [e for e in repair_stream.parse_errors(text.splitlines()) if e["file"] in rewritten]
    if build.get("rc") != 0 and not errors:
        repair.git(worktree, "checkout", "--quiet", "--force", "--detach", head)
        return None
    return ChainRewrite(
        tip=parent,
        paths=rewritten,
        edited=edited,
        errors=errors,
        tool=rewrite.runs["chain"]
        | {"versions": len(renamed), "build": {k: build.get(k) for k in ("rc", "seconds", "log")}},
    )


def _file(path: str) -> str:
    return path if path.endswith(".lean") else module_path(path)


def lint_files(findings: dict, errors: list[dict] | None = None) -> list[str]:
    """The files with a numeric projection finding among ``findings``' compiler errors and the
    streaming build's ``errors`` (``repair_parallel.read_errors``)."""
    files = [_file(str(e.get("path") or "")) for e in findings["compiler_errors"] if LINT in str(e.get("message"))]
    files += [str(e["file"]) for e in errors or [] if e.get("file") and LINT in str(e.get("message"))]
    return [f for f in dict.fromkeys(files) if f.startswith("TauCeti/")]


def replace_findings(findings: dict, rewrite: ChainRewrite) -> dict:
    """``findings`` with the rewritten files' compiler errors, failed modules and build failures
    replaced by the scoped build's errors."""
    from tauceti_queue.diagnostics import module_of

    rewritten = set(rewrite.paths)
    out = {**findings}
    out["compiler_errors"] = [
        e for e in findings["compiler_errors"] if _file(str(e.get("path") or "")) not in rewritten
    ]
    out["compiler_errors"] += [
        {"path": e["file"], "line": e["line"], "column": e["column"], "message": e["message"]} for e in rewrite.errors
    ]
    failed = [m for m in findings["failed_modules"] if _file(str(m)) not in rewritten]
    out["failed_modules"] = list(dict.fromkeys([*failed, *(module_of(e["file"]) for e in rewrite.errors)]))
    out["other"] = [
        f
        for f in findings.get("other") or []
        if not (f.get("kind") == "build" and _file(str(f.get("module"))) in rewritten)
    ]
    return out


def replace_stream_errors(directory: Path, rewrite: ChainRewrite) -> None:
    """The streaming build's error list in ``directory`` (``.tauceti-repair``) with the rewritten
    files' compiler errors replaced by the scoped build's, and a line saying so."""
    from tauceti_queue import repair_stream
    from tauceti_queue.diagnostics import module_of

    path = directory / repair_stream.ERRORS_FILE
    if not path.is_file():
        return
    kept = []
    for line in path.read_text(errors="replace").splitlines():
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            entry = None
        if (
            isinstance(entry, dict)
            and entry.get("file") in rewrite.paths
            and entry.get("kind") in (None, "compiler", repair_stream.RERUN)
        ):
            continue
        kept.append(line)
    count = len(rewrite.errors)
    kept += [
        json.dumps({"module": module_of(e["file"]), **{k: e[k] for k in ("file", "line", "column", "message")}})
        for e in rewrite.errors
    ]
    kept.append(
        json.dumps(
            {
                "event": "numproj",
                "message": f"the worker rewrote the numeric projections in {len(rewrite.paths)} file(s) in the members "
                f"that wrote them and rebuilt those files: {count} error{'s' if count != 1 else ''} left in them, "
                "listed above; their earlier errors are gone",
                "files": rewrite.paths,
            }
        )
    )
    scratch = path.with_name(path.name + ".numproj")
    scratch.write_text("".join(f"{line}\n" for line in kept))
    os.replace(scratch, path)


def _quote(paths: list[str]) -> str:
    return ", ".join(f"`{p}`" for p in paths)


def _note(
    clean: bool,
    complete: bool,
    merged: list[str],
    remaining: list[str],
    untouched: list[str],
    runs: dict,
    copies: dict[str, str],
) -> str:
    if clean:
        return (
            "Resolved mechanically by the repair worker: this commit conflicted with `main`'s rewrite of numeric "
            "projections (`x.1` to named fields). The worker rewrote them with `tauceti-tools numproj` in this "
            f"commit's and its parent's versions of {_quote(merged)} and merged the result onto the clean prefix "
            "with `git merge-file`; no other change was made."
        )
    lines = [
        "Before you started, the worker rewrote numeric projections (`x.1` to named fields, as `main` now has "
        "them) with `tauceti-tools numproj` in this commit's and its parent's versions of the conflicting Lean "
        "files, and merged them onto the clean prefix again."
    ]
    if merged:
        lines.append(f"These merged without conflicts and are in the working tree: {_quote(merged)}. Check them.")
    if remaining:
        lines.append(
            f"These still have conflict markers, now between the clean prefix and the rewritten commit: "
            f"{_quote(remaining)}."
        )
    if untouched:
        lines.append(f"These were not rewritten and keep git's original markers: {_quote(untouched)}.")
    if not complete:
        lines.append(
            "The tool could not rewrite every numeric projection in this commit's version; use named fields "
            "(`.fst`, `.snd`, `.val`, `.property`, ...) for any left in the lines this commit adds:"
        )
        log = Path(runs["commit"].get("log") or "")
        reported = []
        text = ""
        if log.is_file():
            with log.open("rb") as handle:  # the sandboxed run's output: read a bounded head
                text = handle.read(LOG_BYTES).decode(errors="replace")
        for line in text.splitlines():
            if len(reported) == 20:
                break
            for copy, path in copies.items():
                if line.startswith(f"numproj: {copy}:"):
                    reported.append(f"numproj: {path}{line[len('numproj: ') + len(copy) :]}"[:300])
        lines += [f"    {line}" for line in reported[:20]]
    return "\n".join(lines)

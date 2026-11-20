"""A mechanical pre-pass for rebase conflicts caused by renamed numeric projections.

When ``main`` rewrites numeric projections (``x.1`` to ``x.fst``, TauCetiTools ``tauceti-tools
numproj``), a member written against the old ``main`` conflicts wherever its change touches a line
``main`` rewrote. Before the agent runs on such a conflict, ``resolve_conflicts`` calls
:func:`prepass`: for each conflicting Lean file it rewrites the conflicting commit's parent P and the
commit C with the same tool, giving P' and C', and merges with ``git merge-file`` with base P', ours
the clean prefix (already rewritten on the new ``main``) and theirs C'. The tool elaborates each file
against the worktree's warm build, so it runs in the build sandbox like every other build of member
code, on copies of the two versions under ``.lake``; the worktree's sources are not touched until the
merges are done.

The result is mechanical only when every conflicting file merged cleanly and the tool rewrote C's
versions completely (exit 0). Otherwise the merged files, clean or with conflict markers, are the
agent's starting point and :attr:`Prepass.note` tells it what the worker did. Anything unexpected
(the tool missing or failing, a rewrite that changes more than projections and layout, nothing
rewritten) returns None and leaves the worktree as ``apply_conflict`` wrote it.
"""

from __future__ import annotations

import dataclasses
import hashlib
import importlib.util
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
JOBS = 8
# A numeric projection as written: digits after a `.` that follows an identifier or a closing bracket.
# A file without one has nothing for the tool to rewrite.
CANDIDATE = re.compile(r"[\w'\)\]⟩»·]\.\d")
# For `projection_edits`: a numeric projection, the named field replacing it, an identifier's last
# characters (to tell `h1.1` from the literal `1.5`), and a receiver the tool may parenthesize.
_NUMERIC = re.compile(r"\.\d+")
_NAMED = re.compile(r"\.[^\W\d][\w'!?]*")
_IDENT_END = re.compile(r"[\w']+$")
_RECEIVER = re.compile(r"[\w'.$!?«»℘]+")
_BEFORE_PROJECTION = ")]⟩»·"


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


def _blob(worktree: Path, rev: str, path: str) -> bytes | None:
    proc = subprocess.run(
        ["git", "-C", str(worktree), "cat-file", "blob", f"{rev}:{path}"], capture_output=True, check=False
    )
    return proc.stdout if proc.returncode == 0 else None


def projection_edits(before: str, after: str) -> set[tuple[int, int, str, tuple[int, int] | None]] | None:
    """The numproj findings that would turn ``before`` into ``after``, in ``numproj_fix.apply``'s
    form ``(line, col, field, span)``: a numeric projection's digits after an identifier or bracket
    replaced by a field name, with the receiver from ``span[0]`` to the `.` at ``span[1]``
    parenthesized when a `)` is inserted right before it; spaces may become line breaks with
    indentation (``apply`` rewraps). None when ``after`` has any other difference. ``rewrites_only``
    then checks the findings by applying them with the tool's own code."""
    edits: set[tuple[int, int, str, tuple[int, int] | None]] = set()

    def place(i: int) -> tuple[int, int]:
        return before.count("\n", 0, i) + 1, i - (before.rfind("\n", 0, i) + 1)

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
            ident = _IDENT_END.search(after, 0, j)
            if after[j - 1] not in _BEFORE_PROJECTION and (
                ident is None or (ident.group().isdigit() and after[ident.start() - 1 : ident.start()] != ".")
            ):
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
            if not _RECEIVER.fullmatch(before, opened, i) or place(opened)[0] != place(i)[0]:
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
        return handle.read()


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


def prepass(
    worktree: Path,
    prefix: str,
    commit: str,
    paths: list[str],
    env: dict,
    logdir: Path,
    *,
    timeout: float = TIMEOUT_SECONDS,
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
        names = {path: f"{k}.lean" for k, path in enumerate(sides)}
        files: dict[str, list[Path]] = {"parent": [], "commit": []}
        for path, (_, base, theirs) in sides.items():
            for side, data in (("parent", base), ("commit", theirs)):
                target = scratch / f"{side}-{names[path]}"
                target.write_bytes(data)
                files[side].append(target)

        def run(side: str) -> dict:
            log = logdir / f"numproj-{commit[:12]}-{side}.log"
            return _run_tool(worktree, env, copy, files[side], cache, log, timeout)

        with ThreadPoolExecutor(max_workers=2) as pool:  # each side elaborates its own files
            runs = dict(zip(files, pool.map(run, files), strict=True))
        # 0: everything rewritten; 1: some sites left for the author. Anything else is a tool failure.
        if any(runs[side].get("rc") not in (0, 1) for side in files):
            return None
        rewritten: dict[str, tuple[bytes, bytes]] = {}
        changed = False
        for path, (_, base, theirs) in sides.items():
            new_base = _read(directory, f"parent-{names[path]}")
            new_theirs = _read(directory, f"commit-{names[path]}")
            for before, after in ((base, new_base), (theirs, new_theirs)):
                if not rewrites_only(before.decode(), after.decode(errors="replace"), fix):
                    return None
            changed = changed or new_base != base or new_theirs != theirs
            rewritten[path] = (new_base, new_theirs)
        if not changed:
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
                clean, complete, merged, remaining, untouched, runs, {f"{scratch}/commit-{names[p]}": p for p in sides}
            ),
            tool={side: {k: runs[side].get(k) for k in ("rc", "seconds", "timed_out", "log")} for side in runs},
        )
    except (OSError, UnicodeDecodeError, RuntimeError, ValueError):
        return None
    finally:
        if directory is not None:
            os.close(directory)
        if scratch is not None:
            shutil.rmtree(scratch, ignore_errors=True)
    try:
        for path, data in outputs.items():
            (worktree / path).write_bytes(data)
    except OSError:
        from tauceti_queue import repair

        repair.apply_conflict(worktree, prefix, commit)  # back to git's own conflict, as without the pre-pass
        return None
    return outcome


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
        for line in log.read_text(errors="replace").splitlines() if log.is_file() else []:
            for copy, path in copies.items():
                if line.startswith(f"numproj: {copy}:"):
                    reported.append(f"numproj: {path}{line[len('numproj: ') + len(copy) :]}")
        lines += [f"    {line}" for line in reported[:20]]
    return "\n".join(lines)

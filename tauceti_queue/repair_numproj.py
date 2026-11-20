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
import os
import re
import secrets
import shutil
import subprocess
import tempfile
import threading
from pathlib import Path

from tauceti_queue import repair_sandbox

TOOL_FILES = ("numproj_fix.py", "NumProjScan.lean", "local_axioms.py")
SCRATCH = "numproj"  # under the worktree's `.lake`, the only part of the tree the sandbox can write
TIMEOUT_SECONDS = 1800.0
JOBS = 8
# A numeric projection as written: digits after a `.` that follows an identifier or a closing bracket.
# A file without one has nothing for the tool to rewrite.
CANDIDATE = re.compile(r"[\w'\)\]⟩»]\.\d")
# What a projection rewrite may change: the field after a `.`, parentheses around a receiver, and
# whitespace (rewrapped lines). Both sides of a rewrite must agree once these are erased.
_LAYOUT = re.compile(r"[\s()]+")
_FIELD = re.compile(r"\.(?:\d+|[^\W\d][\w'!?]*)")


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


def same_up_to_projections(before: str, after: str) -> bool:
    """Whether ``after`` differs from ``before`` only as a projection rewrite can: fields after a
    `.`, parentheses and whitespace. Anything else means the tool did something it should not."""

    def erase(text: str) -> str:
        return _LAYOUT.sub("", _FIELD.sub(".", text))

    return erase(before) == erase(after)


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
    the worktree is then untouched."""
    tool = tool_dir()
    if tool is None:
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
    lake = worktree / ".lake"
    root = lake / SCRATCH
    cache = root / "cache"  # the compiled scanner, once per toolchain and tree
    scratch = root / f"run-{secrets.token_hex(6)}"
    try:
        cache.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(cache, 0o700)
        copy = scratch / "tool"
        copy.mkdir(parents=True)
        for name in TOOL_FILES:
            shutil.copyfile(tool / name, copy / name)
        files: dict[str, list[Path]] = {"parent": [], "commit": []}
        for path, (_, base, theirs) in sides.items():
            for side, data in (("parent", base), ("commit", theirs)):
                target = scratch / side / path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
                files[side].append(target)
        runs: dict[str, dict] = {}

        def run(side: str) -> None:
            log = logdir / f"numproj-{commit[:12]}-{side}.log"
            runs[side] = _run_tool(worktree, env, copy, files[side], cache, log, timeout)

        threads = [threading.Thread(target=run, args=(side,)) for side in files]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        # 0: everything rewritten; 1: some sites left for the author. Anything else is a tool failure.
        if any(runs.get(side, {}).get("rc") not in (0, 1) for side in files):
            return None
        rewritten: dict[str, tuple[bytes, bytes]] = {}
        changed = False
        for path, (_, base, theirs) in sides.items():
            new_base = (scratch / "parent" / path).read_bytes()
            new_theirs = (scratch / "commit" / path).read_bytes()
            for before, after in ((base, new_base), (theirs, new_theirs)):
                if not same_up_to_projections(before.decode(), after.decode(errors="replace")):
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
        for path, data in outputs.items():
            (worktree / path).write_bytes(data)
        clean = complete and not remaining and not untouched
        return Prepass(
            clean=clean,
            merged=merged,
            remaining=remaining + untouched,
            note=_note(clean, complete, merged, remaining, untouched, runs, scratch),
            tool={side: {k: runs[side].get(k) for k in ("rc", "seconds", "timed_out", "log")} for side in runs},
        )
    except (OSError, UnicodeDecodeError, RuntimeError):
        return None
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def _quote(paths: list[str]) -> str:
    return ", ".join(f"`{p}`" for p in paths)


def _note(
    clean: bool,
    complete: bool,
    merged: list[str],
    remaining: list[str],
    untouched: list[str],
    runs: dict,
    scratch: Path,
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
        reported = [
            line.replace(f"{scratch}/commit/", "")
            for line in (log.read_text(errors="replace").splitlines() if log.is_file() else [])
            if line.startswith(f"numproj: {scratch}/commit/")
        ]
        lines += [f"    {line}" for line in reported[:20]]
    return "\n".join(lines)

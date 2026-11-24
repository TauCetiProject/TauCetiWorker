"""Find and discard built outputs whose bytes differ from the names their Lake traces give them.

Lake records each module output in the module's ``.trace`` under its content hash and, once the
trace's input hash matches, trusts the file on disk without reading it. A write cut short in a
reused tree (leantar writes the ``.trace`` before the outputs; a killed Lean leaves a ``.tmp``
file) therefore leaves a module Lake considers built while an output is truncated or empty. The
repair worker's tree for batch 506 had a 524,288-byte ``.olean.private`` and empty ``.ir.sig``,
``.ir`` and ``.olean.server`` files that way on 24 Nov 2026: the audit driver crashed on them,
and the early publication uploaded them to the repair cache, from which staging restored them.

``damaged`` hashes every root-package output (and the artifact cache's copy of the same name)
with the toolchain's Lake hash, using the shared cache's native helper. ``discard`` removes each
damaged module's build files and cache records, so the next build compiles it from source.
Archives the final build's output map names are checked too. TauCeti does not use postponed
compilation, whose IR outputs are not in the module trace, so those are not covered.
"""

from __future__ import annotations

import concurrent.futures
import json
import os
import re
import stat
import subprocess
import tempfile
from pathlib import Path

from tauceti_worker import expanded_cache

NAME = re.compile(r"[0-9a-f]{16}")
# Lake's module trace output keys and their extensions; "l" is the packed archive at the IR path.
DESCRIPTORS = {
    "o": ("olean", "olean.server", "olean.private"),
    "i": ("ilean",),
    "rs": ("ir.sig",),
    "r": ("ir",),
    "c": ("c",),
    "b": ("bc",),
    "l": ("ltar",),
}
IR_EXTENSIONS = ("c", "bc", "ltar")
MODULE_FILES = {"trace", "olean", "ilean", "ir", "c", "bc", "ltar", "setup", "log"}
CHUNK = 2000  # files per helper call; the helper's own timeout is 45 s
JOBS = 8
DEADLINE = 900  # seconds for the whole check


class Unavailable(RuntimeError):
    """The outputs could not be checked."""


def _regular(path: Path) -> bool:
    try:
        return stat.S_ISREG(path.lstat().st_mode)
    except OSError:
        return False


def expected(worktree: Path) -> list[tuple[Path, object, list[tuple[Path, str]]]]:
    """(module stem, depHash, [(output path, trace name)]) for every root-package module trace."""
    lib, ir = worktree / ".lake/build/lib/lean", worktree / ".lake/build/ir"
    modules = []
    if not lib.is_dir():
        return modules
    for directory, _, files in os.walk(lib):
        for file in files:
            if not file.endswith(".trace"):
                continue
            trace = Path(directory) / file
            stem = trace.relative_to(lib).with_suffix("")
            try:
                value = json.loads(trace.read_text())
            except (OSError, ValueError):
                continue
            outputs = value.get("outputs") if isinstance(value, dict) else None
            if not isinstance(outputs, dict):
                continue
            pairs = []
            for key, extensions in DESCRIPTORS.items():
                names = outputs.get(key)
                for extension, name in zip(extensions, names if isinstance(names, list) else [names], strict=False):
                    if not isinstance(name, str) or not re.fullmatch(NAME.pattern + r"\." + re.escape(extension), name):
                        continue
                    path = (ir if extension in IR_EXTENSIONS else lib) / (str(stem) + "." + extension)
                    if _regular(path):
                        pairs.append((path, name))
            modules.append((stem, value.get("depHash"), pairs))
    return modules


def published_archives(worktree: Path) -> list[tuple[str, str]]:
    """The (input hash, archive name) rows of the final build's native output map, if any."""
    try:
        lines = (worktree / ".lake/outputs.jsonl").read_text().splitlines()
    except OSError:
        return []
    rows = []
    for line in lines[1:]:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if (
            isinstance(row, list)
            and len(row) == 2
            and all(isinstance(v, str) for v in row)
            and NAME.fullmatch(row[0])
            and re.fullmatch(NAME.pattern + r"\.ltar", row[1])
        ):
            rows.append((row[0], row[1]))
    return rows


def native_hashes(worktree: Path, env: dict, paths: list[Path]) -> dict[str, str]:
    """The Lake hash of each path from the shared cache's native helper, in parallel chunks."""
    if not paths:
        return {}
    try:
        program = expanded_cache.native(worktree, env, expanded_cache.root(env))
        chunks = [paths[i : i + CHUNK] for i in range(0, len(paths), CHUNK)]
        with tempfile.TemporaryDirectory(prefix="repair-outputs-") as scratch:
            directories = [Path(scratch) / str(index) for index in range(len(chunks))]
            for directory in directories:
                directory.mkdir()
            pool = concurrent.futures.ThreadPoolExecutor(JOBS)
            try:
                futures = [
                    pool.submit(expanded_cache.hashes, program, chunk, directory)
                    for chunk, directory in zip(chunks, directories, strict=True)
                ]
                hashes = {}
                for future in concurrent.futures.as_completed(futures, timeout=DEADLINE):
                    hashes.update(future.result())
            finally:
                # A failure cancels the chunks not yet started; running ones end within their 45 s.
                pool.shutdown(wait=True, cancel_futures=True)
    except (OSError, ValueError, TypeError, subprocess.SubprocessError, concurrent.futures.TimeoutError) as error:
        raise Unavailable(f"{type(error).__name__}: {error}") from error
    if len(hashes) != len(paths):
        raise Unavailable("incomplete output hashes")
    return hashes


def cache_dir(worktree: Path, env: dict) -> Path:
    """The Lake cache the worktree's builds use: the pool's shared one when ``LAKE_CACHE_DIR`` names it."""
    configured = (env or {}).get("LAKE_CACHE_DIR")
    return Path(configured) if configured else worktree / ".lake/cache"


OFF_VARIABLE = "TAUCETI_REPAIR_OUTPUT_CHECK"  # "off" only in test fixtures whose stub Lake writes fake outputs


def damaged(worktree: Path, env: dict, *, hasher=None) -> list[dict]:
    """Every output (or same-named artifact cache file) whose bytes differ from its trace name."""
    if hasher is None and os.environ.get(OFF_VARIABLE) == "off":
        return []
    worktree = worktree.resolve()
    artifacts = cache_dir(worktree, env) / "artifacts"
    modules = expected(worktree)
    stems = {dep_hash: stem for stem, dep_hash, _ in modules if isinstance(dep_hash, str)}
    checks: list[tuple[Path | None, object, Path, str]] = []
    for stem, dep_hash, pairs in modules:
        for path, name in pairs:
            checks.append((stem, dep_hash, path, name))
            if _regular(artifacts / name):
                checks.append((stem, dep_hash, artifacts / name, name))
    # `lake cache stage` copies the archives the output map names without reading them, and a
    # module's trace often has no "l" entry for its archive.
    for dep_hash, name in published_archives(worktree):
        if _regular(artifacts / name):
            checks.append((stems.get(dep_hash), dep_hash, artifacts / name, name))
    paths = list(dict.fromkeys(path for _, _, path, _ in checks))
    hashes = (hasher or (lambda items: native_hashes(worktree, env, items)))(paths)
    found, seen = [], set()
    for stem, dep_hash, path, name in checks:
        actual = hashes.get(str(path))
        if actual is not None and actual != name.split(".", 1)[0] and (path, name) not in seen:
            seen.add((path, name))
            try:
                size = path.stat().st_size
            except FileNotFoundError:  # another worker discarded the same shared file meanwhile
                continue
            found.append(
                dict(
                    module=str(stem).replace(os.sep, ".") if stem is not None else None,
                    stem=str(stem) if stem is not None else None,
                    dep_hash=dep_hash,
                    path=str(path),
                    expected=name,
                    actual=actual,
                    size=size,
                )
            )
    return found


def discard(worktree: Path, found: list[dict], env: dict | None = None) -> int:
    """Remove each damaged module's build files and the cache records for its input hash.

    A record in a shared pool is only a cache entry: without it the next build restores the
    module from another record or compiles it.
    """
    worktree = worktree.resolve()
    lib, ir = worktree / ".lake/build/lib/lean", worktree / ".lake/build/ir"
    cache = cache_dir(worktree, env or {})
    artifacts, records = cache / "artifacts", cache / "outputs"
    removed: set[str] = set()
    for item in found:
        path = Path(item["path"])
        if path.parent == artifacts:
            path.unlink(missing_ok=True)
        dep_hash = item.get("dep_hash")
        if isinstance(dep_hash, str) and NAME.fullmatch(dep_hash) and records.is_dir():
            for record in records.glob(f"*/{dep_hash}.json"):
                record.unlink(missing_ok=True)
        if item["stem"] is None or item["stem"] in removed:
            continue
        stem = Path(item["stem"])
        removed.add(item["stem"])
        for root in (lib, ir):
            directory = (root / stem).parent
            if not directory.is_dir():
                continue
            for entry in directory.iterdir():
                # `Basic.olean.private`, `Basic.trace`, `Basic.c.hash`, but never `Basic.Sub.olean`.
                if entry.name.startswith(stem.name + ".") and not entry.is_dir():
                    if entry.name[len(stem.name) + 1 :].split(".", 1)[0] in MODULE_FILES:
                        entry.unlink(missing_ok=True)
    return len(removed)


def discard_damaged(worktree: Path, env: dict, *, hasher=None) -> dict:
    """Check a reused tree before building in it; never raises. Returns what was found and removed."""
    try:
        found = damaged(worktree, env, hasher=hasher)
        removed = discard(worktree, found, env) if found else 0
    except (Unavailable, OSError) as error:
        return {"checked": False, "reason": f"{type(error).__name__}: {error}"}
    return {
        "checked": True,
        "damaged_count": len(found),
        "damaged": [{k: item[k] for k in ("module", "path", "expected", "actual", "size")} for item in found[:20]],
        "removed_modules": removed,
    }

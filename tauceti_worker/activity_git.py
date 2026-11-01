"""Exact PR diff counts using an object cache owned by activity preparation."""

import hashlib
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from . import processes


def _git(directory, *args, input=None):
    # Match the ordinary Git transport timeout used by shared_repositories.
    return processes.run(
        ["git", "-c", "maintenance.autoDetach=false", "-c", "gc.autoDetach=false", "--git-dir", str(directory), *args],
        input=input,
        capture_output=True,
        timeout=120,
        check=True,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "true", "SSH_ASKPASS": "true"},
    ).stdout


def _counts(raw):
    chunks, index, files = raw.split(b"\0"), 0, []
    while index < len(chunks) and chunks[index]:
        added, deleted, name = chunks[index].split(b"\t", 2)
        index += 1
        if not name:
            name = chunks[index + 1]  # post-rename filename
            index += 2
        if added == b"-" or deleted == b"-":
            if name.endswith(b".lean"):
                raise ValueError("Binary Lean file has no line count")
            added = deleted = b"0"
        files.append((name, int(added), int(deleted)))
    return (
        {"files": len(files), "added": sum(f[1] for f in files), "deleted": sum(f[2] for f in files)},
        {
            "added": sum(a for n, a, d in files if n.endswith(b".lean")),
            "deleted": sum(d for n, a, d in files if n.endswith(b".lean")),
        },
    )


def measure(directory: Path, repository: str, prs):
    """Return available exact counts; transport failures leave observations unknown."""
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        return {}
    prs = [
        p
        for p in prs
        if type(p.get("number")) is int
        and p["number"] > 0
        and all(isinstance(p.get(k), str) and re.fullmatch(r"[0-9a-f]{40}", p[k]) for k in ("head_sha", "base_sha"))
    ]
    if not prs:
        return {}
    cache = directory / "git" / hashlib.sha256(repository.encode()).hexdigest()
    cache.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    result = {}
    try:
        if not cache.exists():
            staging = Path(tempfile.mkdtemp(prefix=".init-", dir=cache.parent))
            try:
                _git(staging, "init", "--bare", str(staging))
                staging.rename(cache)
            finally:
                shutil.rmtree(staging, ignore_errors=True)
        hashes = sorted({p[k] for p in prs for k in ("head_sha", "base_sha")})
        raw = _git(
            cache, "cat-file", "--batch-check=%(objectname) %(objecttype)", input=("\n".join(hashes) + "\n").encode()
        )
        present = {line.split()[0].decode() for line in raw.splitlines() if line.endswith(b" commit")}
        if present:
            # Real refs preserve objects and advertise haves on later fetches.
            _git(
                cache,
                "update-ref",
                "--stdin",
                input="".join(f"update refs/tauceti-loc/{h} {h}\n" for h in sorted(present)).encode(),
            )
        missing = [h for h in hashes if h not in present]
        if missing:
            try:
                _git(
                    cache,
                    "fetch",
                    "--no-tags",
                    "--no-write-fetch-head",
                    "https://github.com/" + repository + ".git",
                    *[f"{h}:refs/tauceti-loc/{h}" for h in missing],
                )
            except (OSError, subprocess.SubprocessError):
                # A failed fetch must not discard comparisons already cached.
                pass
        for p in prs:
            try:
                raw = _git(
                    cache,
                    "diff",
                    "--no-ext-diff",
                    "--no-textconv",
                    "--find-renames",
                    "--numstat",
                    "-z",
                    p["base_sha"] + "..." + p["head_sha"],
                )
                totals, loc = _counts(raw)
                hints = p.get("diff_totals")
                if isinstance(hints, dict) and any(
                    type(v) is int and totals[k] != v for k, v in hints.items() if k in totals
                ):
                    continue
                result[p["number"]] = {**loc, "head_sha": p["head_sha"]}
            except (OSError, ValueError, subprocess.SubprocessError):
                continue
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    return result

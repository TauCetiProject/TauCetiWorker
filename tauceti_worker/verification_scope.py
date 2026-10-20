"""Local actual-source scope contract shared with the V0 prompt from issue #305."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

CANONICAL = re.compile(
    r"^(?:https://(?:[^@/]+@)?github\.com/|ssh://git@github\.com/|git@github\.com:)"
    r"TauCetiProject/TauCeti(?:\.git)?/?$",
    re.I,
)
MODULE = re.compile(r"^TauCeti(/[A-Za-z_][A-Za-z0-9_']*)+\.lean$")
PINS = {"lake-manifest.json", "lean-toolchain", "lakefile.toml", "lakefile.lean"}
POLICIES = {"ordinary", "full-lint", "lint-repair", "bump"}


def git(root: Path, *args: str) -> bytes:
    return subprocess.run(
        ["git", "--no-optional-locks", "-C", str(root), *args], check=True, capture_output=True
    ).stdout


def local_main(root: Path) -> str:
    """Resolve only a locally available canonical remote. Never fetch or call GitHub."""
    for remote in git(root, "remote").decode().splitlines():
        if CANONICAL.fullmatch(git(root, "config", "--get", f"remote.{remote}.url").decode().strip()):
            try:
                return git(root, "rev-parse", "--verify", f"refs/remotes/{remote}/main^{{commit}}").decode().strip()
            except subprocess.CalledProcessError:
                continue
    raise ValueError("no local canonical TauCetiProject/TauCeti main ref")


def untracked(root: Path) -> set[str]:
    raw = git(root, "ls-files", "-z", "--others", "--exclude-standard", "--", ".")
    raw += git(root, "ls-files", "-z", "--others", "--ignored", "--exclude-standard", "--", "TauCeti")
    return {p.decode(errors="surrogateescape") for p in raw.split(b"\0") if p}


def scope(root: Path, base: str, policy: str) -> dict:
    if policy not in POLICIES:
        raise ValueError(f"unknown local verification scope policy {policy!r}")
    result = dict(mode="unknown", reason="", modules=[], changed=[], base=base, merge_base=None, policy=policy)
    try:
        if git(root, "rev-parse", "--is-shallow-repository").strip() != b"false":
            raise ValueError("shallow history")
        merge = git(root, "merge-base", base, "HEAD").decode().strip()
        result["merge_base"] = merge
        raw = git(
            root,
            "diff",
            "--no-ext-diff",
            "--no-textconv",
            "--no-renames",
            "--no-relative",
            "--name-only",
            "-z",
            merge,
            "--",
        )
        changed = {p.decode(errors="surrogateescape") for p in raw.split(b"\0") if p} | untracked(root)
        result["changed"] = sorted(changed)
    except (subprocess.CalledProcessError, ValueError) as error:
        result["reason"] = str(error)
        return result
    reasons = []
    modules = []
    for name in sorted(changed):
        if name in PINS:
            reasons.append(f"dependency, toolchain or lakefile change: {name}")
        elif name.endswith(".lean"):
            if not MODULE.fullmatch(name):
                reasons.append(f"Lean file outside the module inventory: {name}")
            elif not (root / name).is_file():
                reasons.append(f"deleted module: {name}")
            else:
                modules.append(name[:-5].replace("/", "."))
    if policy != "ordinary":
        reasons.append(f"{policy} policy")
    result.update(
        mode="full" if reasons else "scoped",
        reason=reasons[0] if reasons else f"against {merge}",
        modules=sorted(set(modules)),
    )
    return result

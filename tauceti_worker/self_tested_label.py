"""Label a round's verified pushes `self-tested` from the worker, after the agent exits.

Once a host round's agent has exited, the
worker labels each head the round published through git-safe-push itself, but only when:

- the self-tested route is enabled (`TAUCETI_SELF_TESTED=1`) for TauCetiProject/TauCeti;
- the selected forge still reports the pushed commit as the PR head;
- the change is ordinary mathematics entirely under `TauCeti/` (no pin, lakefile or other
  infrastructure change; no full-lint or lint-repair request, label or branch); and
- this round left a successful, complete `tauceti-verify` receipt (every stage, policy
  `ordinary`) whose recorded sources are exactly the pushed commit's tree.

The label is a persistent author declaration. Reviews and admission check eligibility inline;
all staging builds and audits still run. The worker's optional automatic labeling uses the same
receipt and source checks on either forge, without a picker or status-publication protocol.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path

from .bump_attestation import LABEL
from .verification import SCHEMA, STAGES, digest
from .verification_scope import git, local_main


def enabled() -> str:
    """Why the worker must not label anything, or "" when the self-tested route is on."""
    from .worker_sources import canonical_mathematics_source

    if os.environ.get("TAUCETI_SELF_TESTED", "0") != "1":
        return "TAUCETI_SELF_TESTED is not 1"
    if not canonical_mathematics_source():
        return "repository is not TauCetiProject/TauCeti"
    return ""


def _labels(live) -> set[str]:
    return {str(label.get("name", "")).lower() for label in (live or {}).get("labels", []) or []}


def _live_reason(live, head: str) -> str:
    """Why the live PR is not an ordinary open PR at `head`, or "" when it is."""
    from tauceti_tools.self_tested import EXCLUDED_LABELS

    from .agents import scope_policy

    if not live:
        return "PR unreadable"
    ref = str(live.get("head", {}).get("ref", ""))
    checks = (
        (live.get("state") == "open", "PR not open"),
        (not live.get("draft"), "PR is a draft"),
        (live.get("base", {}).get("ref") == "main", "base is not main"),
        (live.get("head", {}).get("sha") == head, "PR head moved since the push"),
        (not ref.startswith("bump-mathlib/"), "automatic bump branch"),
        (
            scope_policy(sorted(_labels(live)), ref) == "ordinary" and not _labels(live) & EXCLUDED_LABELS,
            "full-lint or lint-repair request",
        ),
    )
    return next((why for ok, why in checks if not ok), "")


def _tree(checkout: Path, commit: str) -> dict[str, tuple[str, str]]:
    """path -> (git mode, blob id) for every entry of `commit`'s tree."""
    out = {}
    for row in git(checkout, "ls-tree", "-r", "-z", "--full-tree", commit).split(b"\0"):
        if not row:
            continue
        meta, _, path = row.partition(b"\t")
        mode, kind, oid = meta.decode().split()
        out[path.decode(errors="surrogateescape")] = (mode, oid if kind == "blob" else "")
    return out


def _blobs(checkout: Path, oids) -> dict[str, bytes]:
    """The contents of each blob id, read in one `git cat-file --batch`."""
    oids = sorted(set(oids))
    if not oids:
        return {}
    result = subprocess.run(
        ["git", "--no-optional-locks", "-C", str(checkout), "cat-file", "--batch"],
        input="".join(f"{oid}\n" for oid in oids).encode(),
        check=True,
        capture_output=True,
    ).stdout
    out, at = {}, 0
    for _ in oids:
        header_end = result.index(b"\n", at)
        got, _kind, size = result[at:header_end].decode().split()
        start = header_end + 1
        out[got] = result[start : start + int(size)]
        at = start + int(size) + 1
    return out


def commit_sources(checkout: Path, commit: str, base: str) -> dict:
    """The verifier's source inventory for a clean checkout of `commit` verified against `base`.

    Mirrors `verification.inventory`: every path of the commit and of the base, with each
    file's bytes; a base path the commit removes is missing. File modes are compared only by
    their executable bit, which is all git records. File symlinks bind both their link text
    and the contents of their committed target. External targets, directory links and
    cycles cannot be reconstructed from the commit and never match."""
    tree, base_names = _tree(checkout, commit), set(_tree(checkout, base))
    contents = _blobs(checkout, (oid for _, oid in tree.values() if oid))
    directories = {""} | {"/".join(name.split("/")[:i]) for name in tree for i in range(1, len(name.split("/")))}

    def target_path(name, target):
        if not target or target.startswith("/") or "\0" in target:
            return None
        parts = name.split("/")[:-1] + target.split("/")
        path = []
        for index, part in enumerate(parts):
            if part == "..":
                if not path:
                    return None
                path.pop()
            elif part not in ("", "."):
                path.append(part)
            # Do not normalize through a symlinked directory or nonexistent
            # prefix: its filesystem resolution need not match lexical '..'.
            if index < len(parts) - 1 and "/".join(path) not in directories:
                return None
        return "/".join(path)

    def linked_blob(name):
        seen = set()
        for _ in range(40):
            if name in seen or name not in tree:
                return None
            seen.add(name)
            mode, oid = tree[name]
            if mode in ("100644", "100755"):
                return oid
            if mode != "120000":
                return None
            name = target_path(name, contents[oid].decode(errors="surrogateescape"))
            if name is None:
                return None
        return None

    out = {}
    for name in sorted(set(tree) | base_names):
        if name not in tree:
            out[name] = {"kind": "missing"}
            continue
        mode, oid = tree[name]
        if mode in ("100644", "100755"):
            sha = hashlib.sha256(contents[oid]).hexdigest()
            out[name] = {"kind": "file", "sha256": sha, "executable": mode == "100755"}
        elif mode == "120000" and (target := linked_blob(name)):
            out[name] = {
                "kind": "symlink",
                "target": contents[oid].decode(errors="surrogateescape"),
                "sha256": hashlib.sha256(contents[target]).hexdigest(),
            }
        else:
            out[name] = {"kind": "unsupported", "mode": mode}
    return out


def _comparable(record) -> dict:
    if not isinstance(record, dict):
        return {"kind": "unreadable"}
    kind = record.get("kind")
    if kind == "file":
        mode = record.get("mode")
        try:
            executable = bool(int(mode or 0) & 0o100)
        except (ValueError, TypeError):
            return {"kind": "unreadable"}
        return {"kind": "file", "sha256": record.get("sha256"), "executable": executable}
    if kind == "missing":
        return {"kind": "missing"}
    if kind == "symlink":
        return {"kind": "symlink", "target": record.get("target"), "sha256": record.get("sha256")}
    return {"kind": f"unsupported {kind}"}


def _complete(receipt) -> bool:
    if not isinstance(receipt, dict):
        return False
    phases = receipt.get("phases")
    requested, scope = receipt.get("requested_phases"), receipt.get("scope")
    return bool(
        receipt.get("schema") == SCHEMA
        and receipt.get("status") == "success"
        and isinstance(requested, list)
        and all(isinstance(name, str) for name in requested)
        and sorted(requested) == sorted(STAGES)
        and isinstance(phases, dict)
        and all(isinstance(phases.get(s), dict) and phases[s].get("status") == "success" for s in STAGES)
        and isinstance(scope, dict)
        and scope.get("policy") == "ordinary"
    )


def matching_receipt(state: Path, checkout: Path, commit: str, since: float) -> tuple[dict | None, str]:
    """This round's successful complete ordinary receipt for exactly `commit`'s sources.

    Returns (receipt, base) when one exists, else (None, why)."""
    paths = []
    for path in (state / "verification").glob("*/receipt.json"):
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        if mtime >= since:
            paths.append((mtime, path))
    if not paths:
        return None, "no verification this round"
    expected, seen = {}, "no successful complete ordinary verification"
    for _, path in sorted(paths, reverse=True):
        try:
            receipt = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if not _complete(receipt):
            continue
        try:
            inputs_ref = receipt["inputs"]
            inputs = json.loads(Path(inputs_ref["path"]).read_text())
            if digest(inputs) != inputs_ref["sha256"] or receipt.get("fingerprint") != inputs_ref["sha256"]:
                continue
            base, sources = str(inputs["base"]), inputs["sources"]
            if base not in expected:
                expected[base] = commit_sources(checkout, commit, base)
        except (OSError, ValueError, KeyError, TypeError, subprocess.CalledProcessError):
            continue
        seen = "verified sources differ from the pushed commit"
        if isinstance(sources, dict) and {k: _comparable(v) for k, v in sources.items()} == expected[base]:
            return receipt, base
    return None, seen


def _changed_paths(checkout: Path, base: str, commit: str) -> list[str]:
    merge = git(checkout, "merge-base", base, commit).decode().strip()
    raw = git(checkout, "diff", "--no-ext-diff", "--no-renames", "--name-only", "-z", merge, commit, "--")
    return sorted(p.decode(errors="surrogateescape") for p in raw.split(b"\0") if p)


def label_push(w, stage: str, pr: int, commit: str, since: float) -> str:
    """Label one pushed head `self-tested` when it qualifies; return the one-line outcome."""
    from tauceti_tools.self_tested import allowed_paths

    if why := enabled():
        return f"not eligible ({why})"
    if stage == "lint-repair":
        return "not eligible (lint-repair round)"
    live = w.gh.live_pull(pr)
    if not live:
        return "not eligible (PR unreadable)"
    if why := _live_reason(live, commit):
        return f"not eligible ({why})"
    if LABEL in _labels(live):
        return "already labelled"
    checkout = w.cfg.checkout
    receipt, base = matching_receipt(Path(w.cfg.state), checkout, commit, since)
    if receipt is None:
        return f"no matching receipt ({base})"
    try:
        # Judge the change against canonical main, never the base the receipt chose to verify.
        changed = _changed_paths(checkout, local_main(checkout), commit)
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        return f"not eligible (changed paths unreadable: {error})"
    if not allowed_paths(changed):
        outside = next((p for p in changed if not p.startswith("TauCeti/")), "no changed paths")
        return f"not eligible (changes outside TauCeti/: {outside})"
    # Receipt discovery takes a while; never label a head that moved meanwhile.
    if why := _live_reason(w.gh.live_pull(pr), commit):
        return f"not eligible ({why})"
    if not w.gh.add_label(pr, LABEL):
        return "not labelled: adding the label failed"
    if why := _live_reason(w.gh.live_pull(pr), commit):
        return f"self-tested stopped for {commit[:12]}: {why}"
    return f"labelled {commit[:12]}: persistent self-tested label applied"


def round_pushes(c) -> list[tuple[int | None, str, str]]:
    """(PR, branch, commit) for each head this round published; PR None when it must be looked up.

    A PR round's final push is `progress["published"]` (see `_do_fixlike`); a roadmap round
    records every branch it published, with the fork owner, as `published_branches`."""
    progress = c.progress or {}
    pushes = []
    if c.pr and progress.get("published"):
        pushes.append((c.pr, "", str(progress["published"])))
    for branch, commit in sorted((progress.get("published_branches") or {}).items()):
        pushes.append((None, branch, str(commit)))
    return pushes


def scoped_publications(checkout: Path, scope: str) -> dict[str, str]:
    """branch -> commit for every push git-safe-push recorded under `scope` in `checkout`."""
    prefix = f"refs/tauceti/published/scope/{scope}/"
    try:
        raw = git(checkout, "for-each-ref", "--format=%(refname)%00%(objectname)", prefix).decode()
    except (OSError, subprocess.CalledProcessError):
        return {}
    out = {}
    for line in raw.splitlines():
        ref, _, oid = line.partition("\0")
        if ref.startswith(prefix) and oid:
            out[ref.removeprefix(prefix)] = oid
    return out


def label_round_pushes(w, c, stage: str, since: float) -> list[tuple[int | None, str]]:
    """Apply `label_push` to every head the round published; one (PR, outcome) per push."""
    owner = str((c.progress or {}).get("published_owner") or "")
    results = []
    for pr, branch, commit in round_pushes(c):
        if pr is None:
            if why := enabled():
                results.append((None, f"{branch}: not eligible ({why})"))
                continue
            pr = w.gh.pull_for_head(owner, branch) if owner else None
            if pr is None:
                results.append((None, f"{branch}: not eligible (no open PR for {owner or '?'}:{branch})"))
                continue
        results.append((pr, label_push(w, stage, pr, commit, since)))
    return results

"""Shared repair machinery of the AI merge queue: preparation, prompt, agent run, chain checks.

Both the historical repair experiments (``repair_experiment.py``, increment 0) and the worker-side
repair task (``repair_task.py``, increment 1) hand an agent a failed attributed chain with warm build
artifacts and measure what comes back. The pieces they share live here:

- chain reading and per-PR comparison (``read_chain``, ``validate_chain``, ``compare_chains``,
  ``owners_of``), a thin layer over ``tauceti_queue.chain`` that also understands Bors's retained
  squash chains, whose PR identity is in the subject rather than a trailer;
- private worktrees and the private artifact pool (``checkout_worktree``, ``lake_env``,
  ``prune_pool``), and warm artifact preparation (``prepare_artifacts``): the published ``main``
  cache at the batch's base plus an incremental build of the batch's own modules, which the plan
  names as the default artifact source;
- the agent prompt (``compose_prompt``): the plan's instruction verbatim, then the diagnostics with
  file-level owners, the constituent commits with their PR identities, the editing recipe and the
  local verification rules;
- the agent run through the worker's host agent path (``agent_environment``, ``run_agent``) and
  the usage accounting from its stream-json transcript (``agent_usage``);
- the result checks (``chain_report``): ``chain.validate`` for membership, history and trailers,
  ``chain.compare`` for append-only messages and provenance, and the list of members whose content
  changed without an appended explanation.

Nothing here publishes anything. Only ``prepare_artifacts`` (the Lake cache service) and the agent
itself touch the network.
"""

from __future__ import annotations

import collections
import dataclasses
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

from tauceti_queue import chain as chain_tool

DEFAULT_REPOSITORY = "TauCetiProject/TauCeti"
CACHE_DOMAIN = os.environ.get("TAUCETI_CACHE_DOMAIN", "cache.taucetiproject.org")
CACHE_SERVICE = os.environ.get("TAUCETI_CACHE_SERVICE", "tauceti-public")

# The plan's agent instruction, used verbatim ("Explanations and PR comments").
AGENT_INSTRUCTION = (
    "Resolve the reported conflicts or CI failures in this batch. Preserve the constituent PRs' intent as "
    "best you can; where they conflict, use your judgment to choose the best alternative or combination. "
    "Update the constituent commits, retaining their source PR identities. When you change a commit's "
    "content, append a very concise explanation of what changed and why to its existing message. If a "
    "contribution cannot be made to pass, reduce it to the part that does, down to an empty commit, and "
    "say so in its message."
)

SUBJECT_PR = re.compile(r"\(#(\d+)\)\s*$")
MODULE_PATH = re.compile(r"TauCeti(?:/[A-Za-z_][A-Za-z0-9_']*)+\.lean")
PRIVATE_DECL = re.compile(r"^_private\.((?:[A-Za-z_][A-Za-z0-9_']*\.)*[A-Za-z_][A-Za-z0-9_']*)\.\d+\.")
TRAILER_LINE = re.compile(r"^(Source-PR|Accepted-Head):\s*(.+?)\s*$")
FAILED_MODULE = re.compile(r"^✖ \[\d+/\d+\] (?:Building|Running) (TauCeti(?:\.[A-Za-z_][A-Za-z0-9_']*)+)")
ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
STAMP = re.compile(r"^\d{4}-\d\d-\d\dT[0-9:.]+Z ")


# ----------------------------------------------------------------------------------------------
# Diagnostics (pure)


def declaration_module(declaration: str) -> str | None:
    """``_private.TauCeti.A.B.0.TauCeti.c`` names its defining module ``TauCeti/A/B.lean``.

    A plain declaration name has no module information; the environment lint driver now emits the
    module path explicitly (staging-triage.md change 4) and older journals only have the private form.
    """
    match = PRIVATE_DECL.match(declaration)
    if not match:
        return None
    parts = match.group(1).split(".")
    if parts[0] != "TauCeti" or len(parts) < 2:
        return None
    return "/".join(parts) + ".lean"


def module_to_path(module: str) -> str:
    return module.replace(".", "/") + ".lean"


def empty_findings() -> dict:
    """The diagnostics structure the prompt consumes; every producer fills a copy of this."""
    return {
        "compiler_errors": [],
        "duplicate_declarations": [],
        "import_collisions": [],
        "lint": {"findings": [], "report": []},
        "failed_modules": [],
        "built_modules": 0,
        "canceled_jobs": 0,
    }


def normalise_findings(raw: dict | None) -> dict:
    """A findings dict with every key present, from a possibly partial one (a service's diagnostics)."""
    findings = empty_findings()
    if not raw:
        return findings
    for key in ("compiler_errors", "duplicate_declarations", "import_collisions", "failed_modules"):
        value = raw.get(key)
        if isinstance(value, list):
            findings[key] = list(value)
    lint = raw.get("lint")
    if isinstance(lint, dict):
        findings["lint"] = {"findings": list(lint.get("findings") or []), "report": list(lint.get("report") or [])}
    for key in ("built_modules", "canceled_jobs"):
        value = raw.get(key)
        if isinstance(value, int):
            findings[key] = value
    return findings


DIAGNOSTIC_KINDS = {
    "compiler": "compiler",
    "compiler_error": "compiler",
    "error": "compiler",
    "audit": "audit",
    "duplicate_declaration": "audit",
    "import_collision": "audit",
    "lint": "lint",
    "environment_linter": "lint",
}
LOCATION = re.compile(r"^(?P<path>\S+?\.lean):(?P<line>\d+):(?P<column>\d+):?\s*(?P<rest>.*)$", re.S)


def findings_from_diagnostics(entries: list | dict | None) -> dict:
    """The findings structure from the controller's task diagnostics.

    The controller hands the repair task a list of findings, each ``{kind, module, message}`` as the
    CI adapter extracted them, and for a rebase conflict one ``{kind: "conflict", source_pr, paths,
    onto, pending, clean_prefix_tip}`` entry. Compiler findings become ``compiler_errors`` (with the
    line and column parsed from the message when it starts with ``path:line:col``), lint findings keep
    their module and message, every other kind is kept under ``other``, and the conflict entry under
    ``conflict``. A dict is taken as an already structured findings record.
    """
    if isinstance(entries, dict):
        findings = normalise_findings(entries)
        findings.setdefault("other", [])
        findings.setdefault("conflict", entries.get("conflict") if isinstance(entries.get("conflict"), dict) else None)
        return findings
    findings = empty_findings()
    findings["other"] = []
    findings["conflict"] = None
    for raw in entries or []:
        if not isinstance(raw, dict):
            continue
        kind = str(raw.get("kind") or "")
        module = str(raw.get("module") or raw.get("path") or "")
        message = str(raw.get("message") or "")
        if kind == "conflict":
            findings["conflict"] = {
                "member": raw.get("source_pr") or raw.get("member"),
                "paths": list(raw.get("paths") or []),
                "onto": raw.get("onto"),
                "pending": list(raw.get("pending") or []),
                "clean_prefix_tip": raw.get("clean_prefix_tip"),
                "message": message,
            }
            continue
        family = DIAGNOSTIC_KINDS.get(kind)
        if family == "compiler":
            entry = {"path": module, "line": raw.get("line", 0), "column": raw.get("column", 0), "message": message}
            located = LOCATION.match(message)
            if located and (
                not module or located.group("path").endswith(module) or module.endswith(located.group("path"))
            ):
                entry.update(
                    path=module or located.group("path"),
                    line=int(located.group("line")),
                    column=int(located.group("column")),
                    message=located.group("rest").strip(),
                )
            if entry not in findings["compiler_errors"]:
                findings["compiler_errors"].append(entry)
            if module and module not in findings["failed_modules"]:
                findings["failed_modules"].append(module)
        elif family == "lint":
            findings["lint"]["findings"].append(
                {"linter": raw.get("linter") or kind, "declaration": raw.get("declaration") or message, "path": module}
            )
        else:
            findings["other"].append({"kind": kind or "finding", "module": module, "message": message})
    return findings


def findings_modules(findings: dict) -> list[str]:
    modules: dict[str, None] = {}
    for error in findings["compiler_errors"]:
        modules.setdefault(error["path"], None)
    for duplicate in findings["duplicate_declarations"]:
        for module in duplicate["modules"]:
            modules.setdefault(module_to_path(module), None)
    for collision in findings["import_collisions"]:
        modules.setdefault(module_to_path(collision["module"]), None)
        modules.setdefault(module_to_path(collision["other_module"]), None)
    for finding in findings["lint"]["findings"]:
        path = finding.get("path") or (
            declaration_module(finding["declaration"]) if finding.get("declaration") else None
        )
        if path:
            modules.setdefault(path, None)
    return list(modules)


def findings_kinds(findings: dict | None) -> list[str]:
    if not findings:
        return []
    kinds = []
    if findings.get("compiler_errors"):
        kinds.append("compiler")
    if findings.get("duplicate_declarations") or findings.get("import_collisions"):
        kinds.append("audit")
    if (findings.get("lint") or {}).get("findings"):
        kinds.append("lint")
    return kinds


# ----------------------------------------------------------------------------------------------
# Git helpers and chains


def git(clone: Path, *args: str, check: bool = True, env: dict | None = None, input: str | None = None) -> str:
    result = subprocess.run(
        ["git", "-C", str(clone), *args], text=True, capture_output=True, env=env, input=input, check=False
    )
    if check and result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed ({result.returncode}): {result.stderr.strip()}")
    return result.stdout


@dataclasses.dataclass
class ChainCommit:
    sha: str
    parent: str
    tree: str
    parent_tree: str
    pr: int | None
    accepted_head: str | None
    subject: str
    message: str  # full message without the Source-PR/Accepted-Head trailers

    @property
    def empty(self) -> bool:
        return self.tree == self.parent_tree


def _commit_fields(clone: Path, sha: str) -> tuple[str, str, str, str]:
    """(parent, tree, subject, body) for a commit; requires exactly one parent."""
    out = git(clone, "show", "-s", "--format=%P%x00%T%x00%s%x00%B", sha)
    parents, tree, subject, body = out.split("\x00", 3)
    parent_list = parents.split()
    if len(parent_list) != 1:
        raise RuntimeError(f"commit {sha[:12]} has {len(parent_list)} parents; the chain must be single-parent")
    return parent_list[0], tree, subject, body


def split_trailers(message: str) -> tuple[str, dict[str, str]]:
    """The message without its identity trailers, and the trailers.

    The trailers are looked for on any line, not only in the final paragraph: an agent that appends
    its explanation after the existing message leaves them mid-message, where git's own
    `interpret-trailers` no longer recognises them (observed in candidate 1931's repair).
    """
    trailers: dict[str, str] = {}
    kept: list[str] = []
    for line in message.split("\n"):
        match = TRAILER_LINE.match(line)
        if match:
            trailers.setdefault(match.group(1), match.group(2))
            continue
        kept.append(line)
    body = "\n".join(kept)
    body = re.sub(r"\n{3,}", "\n\n", body).rstrip("\n") + "\n"
    return body, trailers


def read_chain(clone: Path, base: str, tip: str) -> list[ChainCommit]:
    """The linear chain base..tip, oldest first, with each commit's PR identity.

    The PR comes from the ``Source-PR`` trailer when present (an attributed chain) and from Bors's
    ``(#N)`` subject suffix otherwise (the retained squash chain).
    """
    shas = git(clone, "rev-list", "--reverse", "--first-parent", f"{base}..{tip}").split()
    chain: list[ChainCommit] = []
    expected_parent = git(clone, "rev-parse", f"{base}^{{commit}}").strip()
    parent_tree = git(clone, "rev-parse", f"{base}^{{tree}}").strip()
    for sha in shas:
        parent, tree, subject, body = _commit_fields(clone, sha)
        if parent != expected_parent:
            raise RuntimeError(f"commit {sha[:12]} does not continue the chain (parent {parent[:12]})")
        message, trailers = split_trailers(body)
        pr = None
        if "Source-PR" in trailers:
            match = re.search(r"#(\d+)\s*$", trailers["Source-PR"])
            pr = int(match.group(1)) if match else None
        if pr is None:
            match = SUBJECT_PR.search(subject)
            pr = int(match.group(1)) if match else None
        chain.append(ChainCommit(sha, parent, tree, parent_tree, pr, trailers.get("Accepted-Head"), subject, message))
        expected_parent, parent_tree = sha, tree
    return chain


def validate_chain(
    clone: Path, base: str, tip: str, member_prs: list[int], chain: list[ChainCommit] | None = None
) -> list[str]:
    """Single-parent history on the declared base, one Source-PR per commit, bijection with members."""
    problems: list[str] = []
    try:
        chain = chain if chain is not None else read_chain(clone, base, tip)
    except RuntimeError as error:
        return [str(error)]
    if subprocess.run(["git", "-C", str(clone), "merge-base", "--is-ancestor", base, tip], check=False).returncode != 0:
        problems.append("base is not an ancestor of the tip")
    prs = [c.pr for c in chain]
    if any(p is None for p in prs):
        problems.append("a commit has no Source-PR identity")
    if any(c.accepted_head is None for c in chain):
        problems.append("a commit has no Accepted-Head trailer")
    counts = collections.Counter(p for p in prs if p is not None)
    duplicates = sorted(p for p, n in counts.items() if n > 1)
    if duplicates:
        problems.append(f"PRs with more than one commit: {duplicates}")
    missing = sorted(set(member_prs) - set(counts))
    if missing:
        problems.append(f"members without a commit: {missing}")
    extra = sorted(set(counts) - set(member_prs))
    if extra:
        problems.append(f"commits for non-members: {extra}")
    return problems


def _patch_id(clone: Path, commit: ChainCommit) -> str:
    if commit.empty:
        return "empty"
    patch = git(clone, "diff-tree", "-p", "--no-color", commit.parent, commit.sha)
    out = git(clone, "patch-id", "--stable", input=patch)
    return out.split()[0] if out.split() else "empty"


def _diffstat(clone: Path, commit: ChainCommit) -> dict[str, int]:
    stat = {"files": 0, "insertions": 0, "deletions": 0}
    if commit.empty:
        return stat
    out = git(clone, "diff-tree", "--shortstat", "--no-color", commit.parent, commit.sha).strip()
    for pattern, key in (
        (r"(\d+) files? changed", "files"),
        (r"(\d+) insertions?", "insertions"),
        (r"(\d+) deletions?", "deletions"),
    ):
        match = re.search(pattern, out)
        if match:
            stat[key] = int(match.group(1))
    return stat


def compare_chains(clone: Path, before: list[ChainCommit], after: list[ChainCommit]) -> list[dict]:
    """Per-PR provenance: clean (identical patch) or edited; append-only message check; shrink events.

    Membership is compared by PR identity, not position.
    """
    before_by_pr = {c.pr: c for c in before if c.pr is not None}
    after_by_pr = {c.pr: c for c in after if c.pr is not None}
    rows: list[dict] = [
        {"pr": None, "status": "unidentified", "after": c.sha, "subject": c.subject} for c in after if c.pr is None
    ]
    for pr in sorted(set(before_by_pr) | set(after_by_pr)):
        old, new = before_by_pr.get(pr), after_by_pr.get(pr)
        if old is None or new is None:
            rows.append({"pr": pr, "status": "missing_before" if old is None else "missing_after"})
            continue
        old_id, new_id = _patch_id(clone, old), _patch_id(clone, new)
        changed = old_id != new_id
        old_stat, new_stat = _diffstat(clone, old), _diffstat(clone, new)
        old_message = old.message.rstrip()
        new_message = new.message.rstrip()
        preserved = new_message.startswith(old_message)
        explanation = new_message[len(old_message) :].strip() if preserved else None
        # A shrink event: the commit became empty, lost whole files, or lost more than half of its
        # insertions (at least twenty lines) while its content changed.
        shrink = (new.empty and not old.empty) or (
            changed
            and not new.empty
            and (
                new_stat["files"] < old_stat["files"]
                or old_stat["insertions"] - new_stat["insertions"] >= max(20, old_stat["insertions"] // 2)
            )
        )
        rows.append(
            {
                "pr": pr,
                "status": "edited" if changed else "clean",
                "before": old.sha,
                "after": new.sha,
                "empty_before": old.empty,
                "empty_after": new.empty,
                "diffstat_before": old_stat,
                "diffstat_after": new_stat,
                "message_preserved": preserved,
                "explanation": explanation,
                "shrink": shrink,
            }
        )
    return rows


def owners_of(clone: Path, chain: list[ChainCommit], path: str) -> list[int]:
    """Constituent PRs whose commit touches ``path`` (file-level ownership, as Bors attributes it)."""
    if not chain:
        return []
    shas = set(git(clone, "rev-list", f"{chain[0].parent}..{chain[-1].sha}", "--", path).split())
    return [c.pr for c in chain if c.sha in shas and c.pr is not None]


def pr_number(source_pr: str) -> int:
    """``owner/repo#N`` to ``N``."""
    return int(source_pr.rsplit("#", 1)[1])


def chain_report(
    clone: Path, base: str, prior_tip: str | None, tip: str, members: list[str], *, prior_base: str | None = None
) -> dict:
    """The queue's result checks on a proposed chain, as one JSON-ready record.

    ``validate`` is ``chain.validate`` (members by PR identity, single-parent history on ``base``,
    trailers, count); ``compare`` is ``chain.compare`` against the prior chain ``prior_base..prior_tip``
    (append-only messages, provenance per member), the controller's own order of checks. ``unexplained``
    lists the members whose content changed without an appended explanation, which the plan says the
    same attempt must add. ``ok`` is the validation verdict together with the append-only check; a
    missing explanation alone does not make it false.
    """
    prior_base = prior_base or base
    record: dict = {"base": base, "tip": tip, "prior_base": prior_base, "prior_tip": prior_tip}
    problems: list[str] = []
    try:
        validation = chain_tool.validate(clone, base, tip, members)
        record["validate"] = validation.to_dict()
        problems += [f"{v.code}: {v.detail}" for v in validation.violations]
    except chain_tool.ChainError as error:
        record["validate"] = {"ok": False, "error": str(error)}
        problems.append(f"validate: {error}")
    unexplained: list[str] = []
    edited: list[str] = []
    if prior_tip and prior_tip != tip:
        try:
            comparison = chain_tool.compare(clone, prior_base, prior_tip, base, tip)
            record["compare"] = comparison.to_dict()
            problems += [f"{v.code}: {v.detail}" for v in comparison.violations]
            for member in comparison.members:
                if member.provenance == chain_tool.EDITED:
                    edited.append(member.source_pr)
                if member.content_changed and member.message_preserved and not member.explanation.strip():
                    unexplained.append(member.source_pr)
        except chain_tool.ChainError as error:
            record["compare"] = {"ok": False, "error": str(error)}
            problems.append(f"compare: {error}")
    elif prior_tip == tip:
        record["compare"] = {"ok": True, "unchanged": True}
    record["edited"] = edited
    record["unexplained"] = unexplained
    record["problems"] = problems
    record["ok"] = not problems
    return record


# ----------------------------------------------------------------------------------------------
# Host state, worktrees and the artifact pool


def host_state(where: Path | None = None) -> dict:
    load = os.getloadavg()
    target = where if where is not None and where.exists() else Path.home()
    usage = shutil.disk_usage(str(target))
    return {
        "time": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
        "load_1m": round(load[0], 1),
        "load_5m": round(load[1], 1),
        "load_15m": round(load[2], 1),
        "disk_free_gb": round(usage.free / 1e9, 1),
        "cpus": os.cpu_count(),
    }


def create_worktree(clone: Path, sha: str, path: Path) -> None:
    if path.exists():
        remove_worktree(clone, path)
    path.parent.mkdir(parents=True, exist_ok=True)
    git(clone, "worktree", "add", "--detach", str(path), sha)


def remove_worktree(clone: Path, path: Path) -> None:
    if path.exists():
        lake = path / ".lake"
        if lake.is_dir() and not lake.is_symlink():
            packages = lake / "packages"
            if packages.is_symlink():
                packages.unlink()
            shutil.rmtree(lake, ignore_errors=True)
        git(clone, "worktree", "remove", "--force", str(path), check=False)
        if path.exists():
            shutil.rmtree(path, ignore_errors=True)
    git(clone, "worktree", "prune", check=False)


def checkout_worktree(clone: Path, sha: str, path: Path) -> bool:
    """A detached worktree at ``sha``; an existing one is re-pointed so its ``.lake`` outputs stay warm.

    Returns True when a worktree was reused. The worktree belongs to the harness alone: tracked
    changes left by a previous agent are discarded (its result is already retained under a ref) and
    untracked files other than ``.lake`` are removed.
    """
    if path.is_dir() and git(path, "rev-parse", "--is-inside-work-tree", check=False).strip() == "true":
        git(path, "checkout", "--quiet", "--force", "--detach", sha)
        git(path, "clean", "-fdq", "-e", ".lake", check=False)
        return True
    create_worktree(clone, sha, path)
    return False


def link_shared_packages(worktree: Path, seed_packages: Path) -> None:
    """Share one extracted Mathlib (and the other dependencies) across worktrees.

    When every batch pins the same Mathlib revision and toolchain, the seed's ``.lake/packages`` is
    read-only for every build; this stands in for the workers' shared Mathlib view
    (docs/shared-mathlib.md) without the overlay.
    """
    lake = worktree / ".lake"
    lake.mkdir(exist_ok=True)
    target = lake / "packages"
    if target.is_symlink() or target.exists():
        if target.is_symlink():
            target.unlink()
        else:
            shutil.rmtree(target)
    target.symlink_to(seed_packages)


def lake_env(root: Path, base_env: dict | None = None) -> dict:
    """The workers' artifact-cache environment: the public service and a private artifact pool."""
    root.mkdir(parents=True, exist_ok=True)
    config = root / "lake.toml"
    if not config.exists():
        config.write_text(
            f'[[cache.service]]\nname = "{CACHE_SERVICE}"\nkind = "s3"\n'
            f'artifactEndpoint = "https://{CACHE_DOMAIN}/artifacts"\n'
            f'revisionEndpoint = "https://{CACHE_DOMAIN}/revisions"\n'
        )
    env = dict(os.environ if base_env is None else base_env)
    env["LAKE_CONFIG"] = str(config)
    env["LAKE_CACHE_DIR"] = str(root / "lake-cache")
    env["LAKE_ARTIFACT_CACHE"] = "1"
    env["LAKE_RESTORE_ARTIFACTS"] = "1"
    env.pop("TAUCETI_PR_CACHE_MISS", None)
    return env


LAKE_ENV_KEYS = ("LAKE_CONFIG", "LAKE_CACHE_DIR", "LAKE_ARTIFACT_CACHE", "LAKE_RESTORE_ARTIFACTS")


def prune_pool(
    root: Path, shared: Path | None = None, *, drop_revisions: tuple[str, ...] = (), newer_than: float | None = None
) -> dict:
    """Drop built outputs from the private artifact pool, keeping the published base mappings.

    Lake stores every artifact a build produces into ``LAKE_CACHE_DIR`` (hardlinked with the worktree's
    ``.lake/build``), so a 110-PR candidate adds about 13 GB per build. After a base group is finished
    the pool keeps only artifacts named by a ``revisions/*/*.jsonl`` mapping or shared by hardlink with
    the seed cache (``~/.cache/lake``), and ``outputs/`` entries whose artifact is gone are removed.
    ``drop_revisions`` also forgets the mappings of bases that are finished with (their artifacts are
    then pruned too); ``newer_than`` protects artifacts a running build may still be writing. Freed
    bytes count only files with no other hardlink: a built output also linked from a worktree frees
    nothing until that worktree goes.
    """
    pool = root / "lake-cache"
    shared = shared or Path.home() / ".cache" / "lake"
    dropped = 0
    for mapping in (pool / "revisions").glob("*/*.jsonl"):
        if any(mapping.stem.startswith(rev) for rev in drop_revisions if rev):
            mapping.unlink()
            dropped += 1
    referenced: set[str] = set()
    for mapping in (pool / "revisions").glob("*/*.jsonl"):
        for line in mapping.read_text(errors="replace").splitlines():
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, list) and len(row) >= 2 and isinstance(row[1], str):
                referenced.add(row[1])
    removed = removed_bytes = kept = 0
    artifacts = pool / "artifacts"
    if artifacts.is_dir():
        for path in artifacts.iterdir():
            if path.name in referenced:
                kept += 1
                continue
            twin = shared / "artifacts" / path.name
            try:
                if twin.exists() and os.path.samefile(path, twin):
                    kept += 1
                    continue
                stat = path.stat()
                if newer_than is not None and stat.st_mtime >= newer_than:
                    kept += 1
                    continue
                path.unlink()
                removed += 1
                removed_bytes += stat.st_size if stat.st_nlink == 1 else 0
            except OSError:
                continue
    stale = 0
    outputs = pool / "outputs"
    if outputs.is_dir():
        for entry in outputs.glob("*/*.json"):
            try:
                data = json.loads(entry.read_text()).get("data")
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(data, str) and not (artifacts / data).exists():
                entry.unlink(missing_ok=True)
                stale += 1
    return {
        "removed": removed,
        "freed_gb": round(removed_bytes / 1e9, 2),
        "kept": kept,
        "stale_outputs_removed": stale,
        "revisions_dropped": dropped,
    }


def run_logged(argv: list[str], cwd: Path, log: Path, env: dict | None = None, timeout: float | None = None) -> dict:
    """Run a command with output to ``log``; return rc, seconds and the host load around it."""
    before = host_state(cwd)
    started = time.monotonic()
    log.parent.mkdir(parents=True, exist_ok=True)
    timed_out = False
    with log.open("ab") as handle:
        handle.write(f"$ {' '.join(argv)}\n".encode())
        handle.flush()
        try:
            proc = subprocess.run(
                argv, cwd=cwd, env=env, stdout=handle, stderr=subprocess.STDOUT, timeout=timeout, check=False
            )
            rc = proc.returncode
        except subprocess.TimeoutExpired:
            rc, timed_out = 124, True
        except OSError as error:
            handle.write(f"{error}\n".encode())
            rc = 127
    return {
        "rc": rc,
        "seconds": round(time.monotonic() - started, 1),
        "timed_out": timed_out,
        "load_before": before,
        "load_after": host_state(cwd),
        "log": str(log),
    }


def strip_log(text: str) -> list[str]:
    lines = []
    for raw in text.splitlines():
        line = ANSI.sub("", raw)
        line = STAMP.sub("", line)
        lines.append(line.rstrip())
    return lines


def parse_lake_build_log(text: str) -> dict:
    """Module counts from a `lake build` log: built, replayed/restored, failed and canceled jobs."""
    lines = strip_log(text)
    counts = {"built": 0, "replayed": 0, "fetched": 0, "failed_modules": [], "canceled": 0, "total_jobs": None}
    for line in lines:
        if "] Built TauCeti" in line:
            counts["built"] += 1
        elif "] Replayed TauCeti" in line:
            counts["replayed"] += 1
        elif "] Fetched TauCeti" in line or "] Restored TauCeti" in line or "] Unpacked TauCeti" in line:
            counts["fetched"] += 1
        elif "] Canceled " in line:
            counts["canceled"] += 1
        failed = FAILED_MODULE.match(line)
        if failed and failed.group(1) not in counts["failed_modules"]:
            counts["failed_modules"].append(failed.group(1))
        match = re.search(r"\[\d+/(\d+)\]", line)
        if match:
            counts["total_jobs"] = int(match.group(1))
    return counts


# ----------------------------------------------------------------------------------------------
# Warm artifacts (network: the Lake cache service)


def prepare_artifacts(
    worktree: Path,
    base: str,
    env: dict,
    logdir: Path,
    build_timeout: float,
    *,
    repository: str = DEFAULT_REPOSITORY,
    candidate: str | None = None,
    mathlib_cache: bool = False,
    lake: tuple[str, ...] = ("lake",),
) -> dict:
    """Warm artifacts the way the plan's default source does: main's cache at the base, then an
    incremental build of the batch's modules. Returns timings, module counts and ``available``.

    ``available`` is the plan's "artifacts could be prepared": the base mapping was found and
    restored, and the incremental build ran to completion. The build may well fail (a repair task
    exists because modules fail); only a fetch failure, a missing base mapping or a build that did
    not complete (timeout, missing ``lake``) make the artifacts unavailable. ``mathlib_cache`` first
    runs ``lake exe cache get`` for the dependencies, as the workers do before PR repair.
    ``candidate`` probes whether the failed candidate's own outputs were published (measurement only).
    """
    record: dict = {}
    seconds = 0.0
    reasons: list[str] = []
    if mathlib_cache:
        deps = run_logged([*lake, "exe", "cache", "get"], worktree, logdir / "cache-get-mathlib.log", env, 1800)
        record["mathlib_cache"] = deps
        seconds += deps["seconds"]
        if deps["rc"] != 0:
            reasons.append(f"lake exe cache get exited {deps['rc']}")
    fetch = run_logged(
        [*lake, "cache", "get", "--service", CACHE_SERVICE, "--repo", repository, f"--rev={base}"],
        worktree,
        logdir / "cache-get-base.log",
        env=env,
        timeout=1800,
    )
    text = Path(fetch["log"]).read_text(errors="replace")
    fetch["mapping_found"] = fetch["rc"] == 0 and "outputs not found for revision" not in text
    fetch["artifacts_downloaded"] = text.count("downloaded artifact")
    record["base_cache"] = fetch
    seconds += fetch["seconds"]
    if not fetch["mapping_found"]:
        reasons.append(
            f"no published cache mapping for base {base[:12]}"
            if fetch["rc"] == 0
            else f"lake cache get --rev={base[:12]} exited {fetch['rc']}"
        )
    if candidate:
        own = run_logged(
            [
                *lake,
                "cache",
                "get",
                "--service",
                CACHE_SERVICE,
                "--repo",
                repository,
                f"--rev={candidate}",
                "--mappings-only",
            ],
            worktree,
            logdir / "cache-get-candidate.log",
            env=env,
            timeout=600,
        )
        own["published"] = own["rc"] == 0 and "outputs not found for revision" not in Path(own["log"]).read_text(
            errors="replace"
        )
        record["candidate_outputs_published"] = own
        seconds += own["seconds"]
    if not reasons:
        build = run_logged(
            [*lake, "build", "--iofail"], worktree, logdir / "prepare-build.log", env=env, timeout=build_timeout
        )
        build.update(parse_lake_build_log(Path(build["log"]).read_text(errors="replace")))
        record["incremental_build"] = build
        seconds += build["seconds"]
        if build["timed_out"]:
            reasons.append(f"incremental build did not complete within {build_timeout:.0f}s")
        elif build["rc"] == 127:
            reasons.append("lake could not be run")
    record["seconds"] = round(seconds, 1)
    record["available"] = not reasons
    record["unavailable_reason"] = "; ".join(reasons) if reasons else None
    return record


# ----------------------------------------------------------------------------------------------
# Prompt


@dataclasses.dataclass
class PromptContext:
    """What the prompt says about the batch beyond its diagnostics and commits."""

    repository: str
    base: str  # the base the result must be on (for a rebase conflict: the target base)
    kind: str = "repair"  # repair | rebase_conflict
    origin: str = ""  # one sentence: where this batch came from and why it is here
    suspects: list[int] = dataclasses.field(default_factory=list)
    suspect_heads: dict[int, str] = dataclasses.field(default_factory=dict)
    named_modules: list[str] = dataclasses.field(default_factory=list)
    conflict: dict | None = None  # rebase_conflict: {member, paths, onto, previous_base}
    feedback: str | None = None  # a validation report from a rejected attempt
    members: list[dict] = dataclasses.field(
        default_factory=list
    )  # recorded members: source_pr, accepted_head, provenance
    lint_script: str = "scripts/lint-env.sh"
    instruction: str = AGENT_INSTRUCTION  # the plan's instruction; the service sends it with the task


def _pr_list(prs: list[int]) -> str:
    return ", ".join("#" + str(p) for p in prs)


def compose_prompt(context: PromptContext, findings: dict, chain: list[ChainCommit], clone: Path) -> str:
    """The plan's instruction, then the batch's real diagnostics and its constituent commits."""
    base_short = context.base[:12]
    lines = [context.instruction or AGENT_INSTRUCTION, ""]
    conflict = context.conflict if context.kind == "rebase_conflict" else None
    if conflict:
        previous = (conflict.get("previous_base") or "")[:12] or "its previous base"
        lines += [
            "## The batch",
            "",
            f"You are in a private, detached Git worktree of {context.repository}. HEAD is the tip of a batch of "
            f"{len(chain)} constituent commits, one squash commit per pull request, on base `{previous}`. The queue "
            f"needs this batch on `{base_short}` (current `main`) and its scripted rebase conflicted."
            + (f" {context.origin}" if context.origin else ""),
            "Nothing you do here is published: do not push, do not open pull requests, do not comment, do not use `gh`. "
            "Only this worktree and its commits matter.",
            "Do not edit `lakefile.toml`, `lake-manifest.json`, `lean-toolchain`, `scripts/`, `.github/` or anything under `.lake/`.",
            "",
            "## Conflict evidence",
            "",
        ]
        member = conflict.get("member")
        paths = conflict.get("paths") or []
        lines.append(
            f"- replaying {member or 'a member'} onto `{base_short}` conflicted"
            + (" in " + ", ".join(f"`{p}`" for p in paths) if paths else "")
        )
        for path in paths:
            owners = owners_of(clone, chain, path)
            if owners:
                lines.append(f"  - `{path}` is changed by PR {_pr_list(owners)} in this batch")
        if conflict.get("pending"):
            lines.append("- not yet replayed after it: " + ", ".join(str(p) for p in conflict["pending"]))
        if conflict.get("clean_prefix_tip"):
            lines.append(
                f"- the members before it replay cleanly; that prefix is commit `{conflict['clean_prefix_tip'][:12]}`"
            )
        lines.append("")
    else:
        lines += [
            "## The batch",
            "",
            f"You are in a private, detached Git worktree of {context.repository}. HEAD is the tip of a batch of "
            f"{len(chain)} constituent commits, one squash commit per pull request, on base `{base_short}` (the `main` "
            f"of the time)." + (f" {context.origin}" if context.origin else ""),
            "Nothing you do here is published: do not push, do not open pull requests, do not comment, do not use `gh`. "
            "Only this worktree and its commits matter.",
            "Do not edit `lakefile.toml`, `lake-manifest.json`, `lean-toolchain`, `scripts/`, `.github/` or anything under `.lake/`.",
            "",
            "## CI failure evidence",
            "",
            "The staging build ran `lake build --iofail` with `--fail-fast`, so it reports the first independently broken "
            "module; more may appear once that one builds. Audit and lint phases only run after a complete build.",
            "",
        ]
    if findings["compiler_errors"]:
        lines.append("Compiler errors:")
        lines.append("")
        for error in findings["compiler_errors"]:
            owners = owners_of(clone, chain, error["path"])
            owner_text = f" (changed by PR {_pr_list(owners)})" if owners else " (not changed by this batch)"
            lines.append(f"- `{error['path']}:{error.get('line', 0)}:{error.get('column', 0)}`{owner_text}")
            for text in str(error.get("message", "")).splitlines():
                lines.append(f"      {text}")
        lines.append("")
    if findings["failed_modules"] and not findings["compiler_errors"]:
        lines.append("Modules CI failed to build: " + ", ".join(f"`{m}`" for m in findings["failed_modules"]) + ".")
        lines.append("")
    if findings["duplicate_declarations"]:
        lines.append("Audit findings (duplicate declarations across modules; the audit rejects a name declared twice):")
        lines.append("")
        for duplicate in findings["duplicate_declarations"]:
            modules = [module_to_path(m) for m in duplicate["modules"]]
            owners = {m: owners_of(clone, chain, m) for m in modules}
            lines.append(
                f"- `{duplicate['declaration']}` is declared in both "
                + " and ".join(f"`{m}` (PR {_pr_list(owners[m]) or 'unchanged'})" for m in modules)
            )
        lines.append("")
    if findings["import_collisions"]:
        lines.append("Audit findings (import collisions):")
        lines.append("")
        for collision in findings["import_collisions"]:
            module, other = module_to_path(collision["module"]), module_to_path(collision["other_module"])
            lines.append(
                f"- importing `{module}` (PR {_pr_list(owners_of(clone, chain, module)) or 'unchanged'}) "
                f"failed: the environment already contains `{collision['declaration']}` from `{other}` "
                f"(PR {_pr_list(owners_of(clone, chain, other)) or 'unchanged'})"
            )
        lines.append("")
    if findings["lint"]["findings"] or findings["lint"]["report"]:
        lines.append(
            f"Environment lint findings (`{context.lint_script}`, new violations against the grandfathered baseline):"
        )
        lines.append("")
        for finding in findings["lint"]["findings"]:
            path = finding.get("path") or (
                declaration_module(finding["declaration"]) if finding.get("declaration") else None
            )
            owners = owners_of(clone, chain, path) if path else []
            where = f" in `{path}` (PR {_pr_list(owners) or 'unchanged'})" if path else ""
            lines.append(f"- linter `{finding.get('linter')}`: `{finding.get('declaration')}`{where}")
        if findings["lint"]["report"]:
            lines.append("")
            lines.append("Lint report excerpt:")
            lines.append("")
            lines += [f"    {text}" for text in findings["lint"]["report"][:40]]
        lines.append("")
    if findings.get("other"):
        lines.append("Other CI findings:")
        lines.append("")
        for finding in findings["other"]:
            owners = owners_of(clone, chain, finding["module"]) if finding.get("module") else []
            where = f" in `{finding['module']}` (PR {_pr_list(owners) or 'unchanged'})" if finding.get("module") else ""
            lines.append(f"- {finding['kind']}{where}: {finding['message']}")
        lines.append("")
    if context.named_modules:
        lines.append(
            "Modules the journal's diagnostics name: " + ", ".join(f"`{m}`" for m in context.named_modules) + "."
        )
    if context.suspects:
        lines.append(
            "Bors attributed the failure to PR "
            + ", ".join(f"#{p} (accepted head `{context.suspect_heads.get(p, '?')[:12]}`)" for p in context.suspects)
            + "; use this as a hint, not a verdict."
        )
    if context.feedback:
        lines += [
            "",
            "## A previous attempt was rejected",
            "",
            "The queue did not adopt the chain a previous attempt of this task returned. Its validation report:",
            "",
            *[f"    {text}" for text in context.feedback.splitlines()],
            "",
            "Start from HEAD as it is now and produce a chain that passes these checks.",
        ]
    lines += [
        "",
        "## Constituent commits (oldest first)",
        "",
        "Each commit carries `Source-PR:` and `Accepted-Head:` trailers; keep them on the commit that represents that PR.",
        "",
    ]
    for commit in chain:
        lines.append(f"- `{commit.sha[:12]}` #{commit.pr}: {commit.subject}")
    if context.members:
        lines += [
            "",
            "## Recorded members",
            "",
            "The queue validates the result against this member list: exactly one commit per member, identified by "
            "these trailers, in any order.",
            "",
        ]
        for member in context.members:
            provenance = member.get("provenance")
            lines.append(
                f"- `Source-PR: {member.get('source_pr')}`, `Accepted-Head: {member.get('accepted_head')}`"
                + (f" (currently {provenance})" if provenance else "")
            )
    recipe_base = base_short if not conflict else (conflict.get("previous_base") or "")[:12] or "<previous base>"
    lines += [
        "",
        "## Editing constituent commits",
        "",
        "Edit the commit that owns the change rather than adding a repair commit on top. One workable recipe: make the fix "
        "in the working tree, `git add` it, `git commit --fixup=<sha>`, and when the message needs its explanation appended "
        "write the complete original message followed by the explanation into a file and `git commit --allow-empty "
        "--fixup=amend:<sha> -F <file>`; finally `GIT_SEQUENCE_EDITOR=: git rebase -i --autosquash --keep-empty --empty=keep "
        f"{recipe_base}`. Any method is fine as long as the result is a linear chain on `{base_short}` with exactly one commit per "
        "Source-PR trailer, every previous message preserved as a prefix of the new one, and empty commits kept where a "
        "contribution was reduced to nothing. Leave HEAD (detached) at the new chain tip with a clean working tree.",
    ]
    if conflict:
        lines += [
            "",
            f"For the rebase itself: `git rebase --keep-empty --empty=keep --onto {base_short} {recipe_base}` replays the chain "
            f"commit by commit onto `{base_short}`; resolve each conflict inside the constituent commit it stops at, "
            "`git add` the resolution and `git rebase --continue`. A member whose change is already in the new base "
            "becomes an empty commit, never a dropped one.",
        ]
    lines += [
        "",
        "## Verifying locally",
        "",
        "Artifacts are warm: Mathlib and every module of this batch that compiled are already built in `.lake`, so "
        "`lake build --iofail` (whole library, incremental, as CI runs it) rebuilds only the failing modules and their "
        "dependents. Run it in the foreground and judge it by its exit status. Shell commands have a ten-minute limit; if "
        "a build times out, rerun the same command in the same checkout, completed modules are kept. Do not run "
        "`lake exe cache get` or `lake cache get`; do not use `--fail-fast`. "
        f"For lint findings, `bash {context.lint_script}` reproduces the environment lint (it needs the complete build and takes "
        "a while). For duplicate declarations there is no light local audit: make sure the name is declared exactly once "
        "across the two modules (or made private/renamed so both intents survive) and that both modules build. "
        "Do not weaken anything to get green: no `sorry`, no `native_decide`, no new axioms, no silenced linters, no "
        "`maxHeartbeats` overrides.",
        "",
        "Work synchronously to completion in this turn; nothing resumes you. Finish with a short summary of what failed, "
        "what you changed in which PR's commit, and the exit status of the final `lake build --iofail`.",
    ]
    return "\n".join(lines)


def explanation_prompt(unexplained: list[str], chain: list[ChainCommit]) -> str:
    """The short follow-up turn when content changed without an appended explanation."""
    by_pr = {c.pr: c for c in chain if c.pr is not None}
    lines = [
        "The chain at HEAD changes the content of these constituent commits without appending an explanation to "
        "their messages:",
        "",
    ]
    for source_pr in unexplained:
        number = pr_number(source_pr)
        commit = by_pr.get(number)
        lines.append(f"- {source_pr}" + (f": `{commit.sha[:12]}` {commit.subject}" if commit else ""))
    lines += [
        "",
        "Append a very concise explanation of what changed and why to each of these messages, preserving the existing "
        "message text as a prefix and keeping the `Source-PR:` and `Accepted-Head:` trailers. Change nothing else: no "
        "content edits, no reordering, no new commits. Leave HEAD (detached) at the reworded chain tip, still a linear "
        f"chain on `{(chain[0].parent[:12] if chain else '?')}`, with a clean working tree, then stop.",
    ]
    return "\n".join(lines)


# ----------------------------------------------------------------------------------------------
# Agent


def claude_profile():
    from tauceti_worker.agents import resolve_authoring_profile

    return resolve_authoring_profile("claude")


def prepare_claude_config(root: Path) -> Path:
    """An isolated Claude config dir seeded the way workers seed theirs: credentials, not personality."""
    from tauceti_worker.agents import seed_worker_claude_config
    from tauceti_worker.quota import claude_dir

    iso = root / "claude-config"
    iso.mkdir(parents=True, exist_ok=True)
    real = claude_dir(Path(os.environ.get("HOME", os.path.expanduser("~"))))
    seed_worker_claude_config(real, iso)
    for name in (".credentials.json",):
        src = real / name
        if src.exists():
            shutil.copy2(src, iso / name)
    return iso


def isolation_env(root: Path, env_lake: dict | None = None) -> dict:
    """Environment changes that keep a repair agent private: no GitHub credentials, no push, its
    own Claude config dir, and the private artifact pool."""
    changes: dict = {}
    for key in LAKE_ENV_KEYS:
        if env_lake and key in env_lake:
            changes[key] = env_lake[key]
    changes["CLAUDE_CONFIG_DIR"] = str(prepare_claude_config(root))
    changes["GH_TOKEN"] = None
    changes["GITHUB_TOKEN"] = None
    empty_gh = root / "gh-config-empty"
    empty_gh.mkdir(parents=True, exist_ok=True)
    changes["GH_CONFIG_DIR"] = str(empty_gh)
    changes["GIT_CONFIG_COUNT"] = "2"
    changes["GIT_CONFIG_KEY_0"] = "remote.origin.pushurl"
    changes["GIT_CONFIG_VALUE_0"] = str(root / "no-push")
    changes["GIT_CONFIG_KEY_1"] = "advice.detachedHead"
    changes["GIT_CONFIG_VALUE_1"] = "false"
    return changes


def agent_environment(prompt: str, root: Path, env_lake: dict, deadline_seconds: float) -> tuple[list[str], dict]:
    """The worker's exact argv for a Claude host round, plus the experiment's isolation."""
    os.environ["TAUCETI_ROUND_DEADLINE"] = str(time.time() + deadline_seconds)
    from tauceti_worker.agents import host_agent_argv

    argv, env = host_agent_argv(prompt, claude_profile())
    for key, value in isolation_env(root, env_lake).items():
        if value is None:
            env.pop(key, None)
        else:
            env[key] = value
    return argv, env


def run_agent(worktree: Path, argv: list[str], env: dict, logdir: Path, timeout: float) -> dict:
    """Run the agent with its stream-json transcript captured; wall time from handoff to exit."""
    logdir.mkdir(parents=True, exist_ok=True)
    stream = logdir / "agent-stream.jsonl"
    stderr = logdir / "agent-stderr.log"
    before = host_state(worktree)
    started = time.monotonic()
    timed_out = False
    with stream.open("wb") as out, stderr.open("wb") as err:
        proc = subprocess.Popen(argv, cwd=worktree, env=env, stdout=out, stderr=err, start_new_session=True)
        try:
            rc = proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            os.killpg(proc.pid, 15)
            try:
                proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, 9)
                proc.wait()
            rc = 124
    record = {
        "rc": rc,
        "timed_out": timed_out,
        "wall_seconds": round(time.monotonic() - started, 1),
        "stream": str(stream),
        "load_before": before,
        "load_after": host_state(worktree),
    }
    record.update(agent_usage(stream, Path(env.get("CLAUDE_CONFIG_DIR", ""))))
    return record


def price_usage(provider: str, model: str | None, usage: dict) -> float | None:
    """Estimated API cost of one session's token totals from the repository's dated price table."""
    from tauceti_worker.cost_model import _api_cost

    tokens = {k: int(v) for k, v in (usage or {}).items() if isinstance(v, (int, float))}
    if not tokens:
        return None
    value = _api_cost(provider, model, tokens, dt.datetime.now(dt.UTC))
    return round(value, 4) if value is not None else None


def agent_usage(stream: Path, config_dir: Path | None = None) -> dict:
    """Tokens, model and estimated API cost from the stream-json transcript.

    Assistant events repeat the message usage per content block, so usage is counted once per message
    id (as ``cost_model._parse_claude`` does). Cost uses the repository's dated price table through
    ``cost_model._api_cost``; Claude's own ``total_cost_usd`` is kept beside it for comparison.
    """
    from tauceti_worker.cost_model import _api_cost

    totals: collections.defaultdict[str, int] = collections.defaultdict(int)
    per_model: dict[str, collections.defaultdict[str, int]] = {}
    seen: set[str] = set()
    model = None
    result_event: dict = {}
    session_id = None
    if stream.exists():
        with stream.open(errors="replace") as handle:
            for line in handle:
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(event, dict):
                    continue
                session_id = event.get("session_id") or session_id
                if event.get("type") == "assistant":
                    message = event.get("message") or {}
                    usage = message.get("usage") or {}
                    this_model = message.get("model")
                    if isinstance(this_model, str) and this_model != "<synthetic>":
                        model = this_model
                    usage_id = str(message.get("id") or event.get("uuid"))
                    if usage and usage_id not in seen:
                        seen.add(usage_id)
                        bucket = per_model.setdefault(str(this_model), collections.defaultdict(int))
                        for key, value in usage.items():
                            if isinstance(value, (int, float)):
                                totals[key] += int(value)
                                bucket[key] += int(value)
                        creation = usage.get("cache_creation") or {}
                        if isinstance(creation, dict):
                            for suffix, key in (
                                ("1h", "ephemeral_1h_input_tokens"),
                                ("5m", "ephemeral_5m_input_tokens"),
                            ):
                                value = creation.get(key)
                                if isinstance(value, (int, float)):
                                    totals[f"cache_creation_{suffix}_input_tokens"] += int(value)
                                    bucket[f"cache_creation_{suffix}_input_tokens"] += int(value)
                elif event.get("type") == "result":
                    result_event = event
    # The result event's per-model totals are authoritative: assistant events report a message's usage
    # as it streams, so the output count of the first event per message id is a lower bound.
    reported = result_event.get("modelUsage") or {}
    if isinstance(reported, dict) and reported:
        per_model = {}
        totals = collections.defaultdict(int)
        for name, usage in reported.items():
            if not isinstance(usage, dict):
                continue
            bucket = collections.defaultdict(int)
            for source, key in (
                ("inputTokens", "input_tokens"),
                ("outputTokens", "output_tokens"),
                ("cacheReadInputTokens", "cache_read_input_tokens"),
                ("cacheCreationInputTokens", "cache_creation_input_tokens"),
                ("cacheCreationEphemeral5mInputTokens", "cache_creation_5m_input_tokens"),
                ("cacheCreationEphemeral1hInputTokens", "cache_creation_1h_input_tokens"),
            ):
                value = usage.get(source)
                if isinstance(value, (int, float)):
                    bucket[key] += int(value)
                    totals[key] += int(value)
            per_model[name] = bucket
            model = model or name
    when = dt.datetime.now(dt.UTC)
    cost = 0.0
    priced = True
    for name, bucket in per_model.items():
        value = _api_cost("claude", name, dict(bucket), when)
        if value is None:
            priced = False
        else:
            cost += value
    tokens = {
        key: totals.get(key, 0)
        for key in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "output_tokens")
    }
    tokens["total_tokens"] = sum(tokens.values())
    return {
        "model": model,
        "session_id": session_id,
        "tokens": tokens,
        "models": {name: dict(bucket) for name, bucket in per_model.items()},
        "estimated_cost_usd": round(cost, 4) if priced else None,
        "claude_reported_cost_usd": result_event.get("total_cost_usd"),
        "num_turns": result_event.get("num_turns"),
        "result_subtype": result_event.get("subtype"),
        "is_error": result_event.get("is_error"),
        "final_text": (result_event.get("result") or "")[:4000]
        if isinstance(result_event.get("result"), str)
        else None,
    }


def write_record(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(record, indent=1, default=str))
    os.replace(tmp, path)

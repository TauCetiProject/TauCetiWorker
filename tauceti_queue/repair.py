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
- the agent prompt (``compose_prompt``): the plan's instruction verbatim, then what happened in plain
  words, the evidence with the git refs the agent needs (owning commits, the conflicting commit, the
  clean prefix, base and target), the result the queue expects, the editing instruction and the
  local verification; the member listings go to ``.tauceti-repair/members.jsonl`` in the worktree
  with the agent's one check command, ``.tauceti-repair/check``, and its scoped build,
  ``.tauceti-repair/build`` (``repair_tools``), beside them (``write_listings``);
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
from tauceti_queue import repair_sandbox, repair_tools

DEFAULT_REPOSITORY = "TauCetiProject/TauCeti"
CACHE_DOMAIN = os.environ.get("TAUCETI_CACHE_DOMAIN", "cache.taucetiproject.org")
CACHE_SERVICE = os.environ.get("TAUCETI_CACHE_SERVICE", "tauceti-public")

# The plan's agent instruction, used verbatim ("Explanations and PR comments").
AGENT_INSTRUCTION = (
    "Resolve the reported conflicts or CI failures in this batch. Preserve the constituent PRs' intent as "
    "best you can; where they conflict, use your judgment to choose the best alternative or combination. "
    "Update the constituent commits: each squash commit keeps its `Source-PR:` and `Accepted-Head:` trailers "
    "and its subject ending in ` (#N)`, one commit per PR, in the same order. When you change a commit's "
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
    onto, pending, clean_prefix_tip, landed_on_base}`` entry (``landed_on_base`` lists the members the
    target already carries, ``{source_pr, commit}`` each). Compiler findings become ``compiler_errors`` (with the
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
                "landed_on_base": [
                    entry
                    for entry in raw.get("landed_on_base") or []
                    if isinstance(entry, dict) and entry.get("source_pr")
                ],
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


MAPPING_MISSING = "outputs not found for revision"  # lake's message for a revision the service has no mapping for


def _fetch_mapping(
    worktree: Path,
    revision: str,
    env: dict,
    logdir: Path,
    *,
    repository: str,
    lake: tuple[str, ...],
    mappings_only: bool,
    service: str | None = None,
) -> dict:
    """One ``lake cache get --rev=<revision>`` (from ``service``, default the public one); the verdict reads this run's output only.

    The log is appended to (``run_logged``), and a task's preparation runs again on every claim of
    the task, so the file holds the output of earlier fetches too. Production repair 2 (2026-11-05)
    judged the rebase target unpublished because an earlier run's "outputs not found" for another
    revision was still in the file, although its own fetch had exited 0 and downloaded the artifacts.
    """
    service = CACHE_SERVICE if service is None else service
    public = service == CACHE_SERVICE
    log = logdir / (f"cache-get-{revision[:12]}.log" if public else f"cache-get-{service}-{revision[:12]}.log")
    offset = log.stat().st_size if log.exists() else 0
    argv = [*lake, "cache", "get", "--service", service, "--repo", repository, f"--rev={revision}"]
    if mappings_only:
        argv.append("--mappings-only")
    fetch = run_logged(argv, worktree, log, env=env, timeout=600 if mappings_only else 1800)
    with log.open("rb") as handle:
        handle.seek(offset)
        text = handle.read().decode(errors="replace")
    fetch["revision"] = revision
    fetch["service"] = service
    fetch["mapping_found"] = fetch["rc"] == 0 and MAPPING_MISSING not in text
    fetch["artifacts_downloaded"] = text.count("downloaded artifact")
    if fetch["mapping_found"]:
        fetch["reason"] = None
    elif MAPPING_MISSING in text:
        fetch["reason"] = f"no published cache mapping for {revision[:12]}"
    else:
        fetch["reason"] = f"lake cache get --rev={revision[:12]} exited {fetch['rc']}"
    if fetch["reason"] and not public:
        fetch["reason"] = f"{service}: {fetch['reason']}"
    return fetch


def first_parent_ancestors(worktree: Path, revision: str, count: int) -> list[str]:
    """Up to ``count`` first-parent ancestors of ``revision``, newest first; empty when the history is
    not in the repository (a shallow fetch) or the commit is a root."""
    if count <= 0:
        return []
    out = git(worktree, "rev-list", "--first-parent", f"--max-count={count}", f"{revision}~1", check=False)
    return [line.strip() for line in out.splitlines() if line.strip()]


def _artifact_candidates(
    worktree: Path, base: str, fallbacks: tuple[str, ...], ancestors: int
) -> list[tuple[str, int | None]]:
    """``(revision, distance)`` to ask the cache for, newest first: ``base`` (distance 0), its first-parent
    ancestors (distance 1, 2, ...), then any fallback not on that walk (distance ``None``)."""
    candidates: list[tuple[str, int | None]] = [(base, 0)]
    for distance, sha in enumerate(first_parent_ancestors(worktree, base, ancestors), 1):
        if sha != base:
            candidates.append((sha, distance))
    seen = {sha for sha, _ in candidates}
    for revision in fallbacks:
        if revision and revision not in seen:
            candidates.append((revision, None))
            seen.add(revision)
    return candidates


def _skipped_summary(attempts: list[dict], base: str) -> list[str]:
    """Why the revisions tried before the restored one (or all of them) were passed over: the requested
    revisions each by name, the walked ancestors as one entry."""
    reasons: list[str] = []
    walked = [a for a in attempts if a["reason"] and a["distance"] not in (0, None)]
    for attempt in attempts:
        if not attempt["reason"]:
            continue
        if attempt["distance"] in (0, None):
            reasons.append(attempt["reason"])
        elif attempt is walked[0]:
            if len(walked) == 1:
                reasons.append(f"{attempt['reason']} (1 commit behind {base[:12]})")
            else:
                failed = sum(1 for a in walked if a["rc"] != 0 and "exited" in a["reason"])
                reasons.append(
                    f"no published cache mapping for the {len(walked)} first-parent ancestors of {base[:12]} "
                    f"from {walked[0]['revision'][:12]} back to {walked[-1]['revision'][:12]}"
                    + (f" ({failed} of the fetches exited nonzero)" if failed else "")
                )
    return reasons


def prepare_artifacts(
    worktree: Path,
    base: str,
    env: dict,
    logdir: Path,
    build_timeout: float,
    *,
    repository: str = DEFAULT_REPOSITORY,
    candidate: str | None = None,
    fallbacks: tuple[str, ...] = (),
    ancestors: int = 0,
    mathlib_cache: bool = False,
    lake: tuple[str, ...] = ("lake",),
    preferred: tuple[tuple[str, str], ...] = (),
    build: bool = True,
) -> dict:
    """Warm artifacts the way the plan's default source does: main's cache at the base, then an
    incremental build of the batch's modules. Returns timings, module counts and ``available``.

    ``available`` is the plan's "artifacts could be prepared": a mapping was found and restored, and
    the incremental build ran to completion. The build may well fail (a repair task exists because
    modules fail); only a fetch failure, no published mapping or a build that did not complete
    (timeout, missing ``lake``) make the artifacts unavailable. ``mathlib_cache`` first runs
    ``lake exe cache get`` for the dependencies, as the workers do before PR repair.

    The cache is asked for the newest revision it has among ``base`` (the revision the build runs
    on), up to ``ancestors`` of its first-parent ancestors walking back from it, and ``fallbacks``
    (further revisions, in order, that are not on that walk: the stale batch base of a rebase whose
    target is older than the walk, or the rebase target of a repair whose base is stale), stopping at
    the first that answers. ``cache_revision`` records which one was restored, ``cache_distance`` how
    many commits behind ``base`` it is (``None`` for a fallback off the walk), ``cache_attempts`` every
    fetch tried and ``cache_skipped`` why the revisions before it were passed over. ``candidate``
    probes whether the failed candidate's own outputs were published (measurement only).
    ``preferred`` are ``(service, revision)`` fetches tried before all of these (the workers' repair
    namespace, ``repair_publish.preferred``); ``cache_service`` records the service of the restored
    mapping. ``build=False`` fetches only: the caller runs the
    incremental build itself (``repair_stream`` streams it to the agent), so ``available`` rests on the
    fetch alone.
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
    attempts: list[dict] = []
    candidates = [(service, revision, None) for service, revision in preferred if revision] + [
        (CACHE_SERVICE, revision, distance)
        for revision, distance in _artifact_candidates(worktree, base, fallbacks, ancestors)
    ]
    for service, revision, distance in candidates:
        fetch = _fetch_mapping(
            worktree, revision, env, logdir, repository=repository, lake=lake, mappings_only=False, service=service
        )
        fetch["distance"] = distance
        attempts.append(fetch)
        seconds += fetch["seconds"]
        if fetch["mapping_found"]:
            break
    found = attempts[-1] if attempts[-1]["mapping_found"] else None
    record["base_cache"] = attempts[-1]
    record["cache_attempts"] = attempts
    record["cache_revision"] = found["revision"] if found else None
    record["cache_distance"] = found["distance"] if found else None
    record["cache_service"] = found["service"] if found else None
    record["cache_skipped"] = _skipped_summary(attempts, base)
    if found is None:
        reasons.append("; ".join(record["cache_skipped"]))
    if candidate:
        own = _fetch_mapping(worktree, candidate, env, logdir, repository=repository, lake=lake, mappings_only=True)
        own["published"] = own["mapping_found"]
        record["candidate_outputs_published"] = own
        seconds += own["seconds"]
    if build and not reasons:
        built = run_logged(
            repair_sandbox.sandboxed_lake([*lake, "build", "--iofail"], worktree, env),
            worktree,
            logdir / "prepare-build.log",
            env=env,
            timeout=build_timeout,
        )
        built.update(parse_lake_build_log(Path(built["log"]).read_text(errors="replace")))
        record["incremental_build"] = built
        seconds += built["seconds"]
        if built["timed_out"]:
            reasons.append(f"incremental build did not complete within {build_timeout:.0f}s")
        elif built["rc"] == 127:
            reasons.append("lake could not be run")
    record["seconds"] = round(seconds, 1)
    record["available"] = not reasons
    record["unavailable_reason"] = "; ".join(reasons) if reasons else None
    return record


# ----------------------------------------------------------------------------------------------
# Prompt


LISTINGS_DIR = ".tauceti-repair"
LISTINGS_FILE = "members.jsonl"  # one JSON object per member, in chain order
CHECK_FILE = "check"  # the agent's one validation command, written beside the listings
STEP_FILE = "step"  # steps the rebase through members already on main, stopping at real conflicts
BUILD_FILE = repair_tools.BUILD_FILE  # builds only the named modules, one Lake build at a time
LANDED_SHOWN = 30  # members already on main listed in the prompt itself; the rest are in the listings file
SHELL_LIMIT_SECONDS = 2 * 3600  # a repair agent's foreground shell limit (the fleet's rounds keep ten minutes)
MEMBER_STATES = ("replayed", "conflicting", "pending")

HARD_RULES = (
    "The aim is that every constituent PR's mathematics lands as its author intended, compiled and checked. "
    "Hard rules: no `sorry`, no `native_decide`, no new axioms, no `set_option` (banned throughout Tau Ceti), "
    "no silenced linters."
)
PROTECTED_PATHS = (
    "`lakefile.toml`, `lake-manifest.json`, `lean-toolchain`, `scripts/`, `.github/` or anything under `.lake/`"
)


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
    conflict: dict | None = (
        None  # rebase_conflict: {member, paths, onto, previous_base, pending, clean_prefix_tip, landed_on_base}
    )
    feedback: str | None = None  # a validation report from a rejected attempt
    members: list[dict] = dataclasses.field(
        default_factory=list
    )  # recorded members: source_pr, accepted_head, provenance, title
    instruction: str = AGENT_INSTRUCTION  # the plan's instruction; the service sends it with the task
    artifacts_cold: bool = False  # no warm artifacts: the agent's first build starts from nothing
    artifacts_reason: str | None = None  # ``prepare_artifacts``'s ``unavailable_reason`` when cold
    listings_path: Path | None = None  # where the member listings and the check script are written
    shell_limit: int = SHELL_LIMIT_SECONDS  # what the prompt says about the foreground shell limit
    rebase_options: str = "--keep-empty --empty=keep --reapply-cherry-picks"  # the replay's git options
    repair_context: object | None = None  # ``repair_context.compute``'s result: ownership, collisions and ripples


def _pr_list(prs: list[int]) -> str:
    return ", ".join("#" + str(p) for p in prs)


def path_to_module(path: str) -> str | None:
    """``TauCeti/A/B.lean`` to ``TauCeti.A.B``; None for anything that is not a Lean source path."""
    return path[:-5].replace("/", ".") if path.endswith(".lean") and "/" in path else None


def listings_path(worktree: Path) -> Path:
    """Where ``compose_prompt`` writes the member listings: ``.tauceti-repair/members.jsonl`` in the
    worktree, with the check script beside it, excluded from Git through the repository's
    ``info/exclude`` (shared by all its worktrees) so no recipe the agent runs, ``git add -A``
    included, can commit them. The ``git clean`` of ``checkout_worktree`` leaves ignored files alone;
    both files are rewritten for every prompt, so a stale copy is overwritten, never read."""
    exclude = git(worktree, "rev-parse", "--git-path", "info/exclude", check=False).strip()
    if exclude:
        exclude_path = Path(exclude) if os.path.isabs(exclude) else worktree / exclude
        pattern = f"/{LISTINGS_DIR}/"
        try:
            existing = exclude_path.read_text() if exclude_path.exists() else ""
            if pattern not in existing.splitlines():
                exclude_path.parent.mkdir(parents=True, exist_ok=True)
                with exclude_path.open("a") as handle:
                    handle.write(("" if not existing or existing.endswith("\n") else "\n") + pattern + "\n")
        except OSError:
            pass  # the listing is still written; the prompt tells the agent the files are ignored
    return worktree / LISTINGS_DIR / LISTINGS_FILE


def _members_by_number(context: PromptContext) -> dict[int, dict]:
    by_number: dict[int, dict] = {}
    for member in context.members:
        try:
            by_number[pr_number(str(member.get("source_pr")))] = member
        except (ValueError, IndexError):
            continue
    return by_number


def conflict_position(context: PromptContext, chain: list[ChainCommit]) -> int | None:
    """The chain index of the conflicting member of a rebase conflict, None when there is none."""
    conflict = context.conflict if context.kind == "rebase_conflict" else None
    member = (conflict or {}).get("member")
    if not member or "#" not in str(member):
        return None
    try:
        number = pr_number(str(member))
    except ValueError:
        return None
    return next((i for i, c in enumerate(chain) if c.pr == number), None)


def landed_on_main(context: PromptContext, chain: list[ChainCommit], clone: Path) -> dict[int, str | None]:
    """The members whose PR has landed on ``main`` outside the queue, with the landing commit when
    known: from the recorded members' ``landed_externally`` flag when the service sends one (a commit
    SHA or a boolean), from the conflict diagnostics' ``landed_on_base`` entries (what the queue's own
    replay found in the target's history), else derived from the commits between the batch's base and
    the target whose subject ends in ``(#N)`` for a member's PR number. Empty for a repair, whose base
    is the target."""
    conflict = context.conflict or {}
    previous = conflict.get("previous_base") if context.kind == "rebase_conflict" else None
    numbers = {c.pr for c in chain if c.pr is not None}
    landed: dict[int, str | None] = {}
    flagged = False
    for number, member in _members_by_number(context).items():
        flag = member.get("landed_externally")
        if flag:
            flagged = True
            if number in numbers:
                landed[number] = flag if isinstance(flag, str) and re.fullmatch(r"[0-9a-f]{40}", flag) else None
    for entry in conflict.get("landed_on_base") or []:
        if not isinstance(entry, dict):
            continue
        try:
            number = pr_number(str(entry.get("source_pr") or ""))
        except ValueError:
            continue
        commit = entry.get("commit")
        if number in numbers and landed.get(number) is None:
            landed[number] = commit if isinstance(commit, str) and re.fullmatch(r"[0-9a-f]{40}", commit) else None
    if not previous or previous[:12] == context.base[:12]:
        return landed
    subjects = git(clone, "log", "--format=%H%x00%s", f"{previous}..{context.base}", check=False)
    for line in subjects.splitlines():
        sha, _, subject = line.partition("\x00")
        match = SUBJECT_PR.search(subject)
        if not match:
            continue
        number = int(match.group(1))
        if number in numbers and (not flagged or number in landed) and landed.get(number) is None:
            landed[number] = sha
    return landed


def member_records(
    context: PromptContext, chain: list[ChainCommit], landed: dict[int, str | None] | None = None
) -> list[dict]:
    """The listings file's records, one per constituent commit in chain order: ``position`` (1-based),
    ``commit`` (the commit's SHA in the chain the task started from), ``pr``, ``title``,
    ``accepted_head``, ``provenance`` (the recorded member's, when known), ``landed_on_main`` (the
    commit that landed the PR on ``main`` outside the queue, else null) and, for a rebase conflict,
    ``state``: ``replayed`` before the conflicting member, ``conflicting`` at it, ``pending`` after."""
    by_number = _members_by_number(context)
    stop = conflict_position(context, chain)
    landed = landed or {}
    records = []
    for index, commit in enumerate(chain):
        member = by_number.get(commit.pr) if commit.pr is not None else None
        record = {
            "position": index + 1,
            "commit": commit.sha,
            "pr": commit.pr,
            "title": (member or {}).get("title") or re.sub(r"\s*\(#\d+\)\s*$", "", commit.subject),
            "accepted_head": commit.accepted_head or (member or {}).get("accepted_head"),
            "provenance": (member or {}).get("provenance"),
            "landed_on_main": (landed.get(commit.pr) or "landed") if commit.pr in landed else None,
        }
        if stop is not None:
            record["state"] = (
                MEMBER_STATES[0] if index < stop else MEMBER_STATES[1] if index == stop else MEMBER_STATES[2]
            )
        records.append(record)
    return records


CHECK_SCRIPT = '''#!/usr/bin/env python3
"""Check the repaired chain in this worktree: prints the first problem, or `ok`.

Written by the queue's repair worker; it verifies what the queue's own validation will: a linear
chain on the target commit, one commit per recorded member in the recorded order, the `Source-PR:`
and `Accepted-Head:` trailers and the ` (#N)` subject suffix kept, every original message preserved
as a prefix of the new one, no rebase in progress and a clean working tree.
"""
import json
import re
import subprocess
import sys
from pathlib import Path

TARGET = {target!r}
REPOSITORY = {repository!r}
HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
TRAILER = re.compile(r"^(Source-PR|Accepted-Head):\\s*(.+?)\\s*$")


def fail(problem: str) -> None:
    print(problem)
    sys.exit(1)


def git(*args: str) -> str:
    result = subprocess.run(["git", "-C", str(ROOT), *args], text=True, capture_output=True)
    if result.returncode:
        fail(f"git {{' '.join(args)}} failed: {{result.stderr.strip()}}")
    return result.stdout


def trailers(message: str) -> dict:
    found = {{}}
    for line in message.splitlines():
        match = TRAILER.match(line)
        if match:
            found.setdefault(match.group(1), []).append(match.group(2))
    return found


members = [json.loads(line) for line in (HERE / "members.jsonl").read_text().splitlines() if line.strip()]
for state in ("rebase-merge", "rebase-apply"):
    path = git("rev-parse", "--git-path", state).strip()
    if (Path(path) if Path(path).is_absolute() else ROOT / path).exists():
        fail("a rebase is still in progress: finish it with `git rebase --continue`")
if git("status", "--porcelain", "--untracked-files=no").strip():
    fail("the working tree has uncommitted changes")
if subprocess.run(["git", "-C", str(ROOT), "merge-base", "--is-ancestor", TARGET, "HEAD"], check=False).returncode:
    fail(f"HEAD is not on top of the target commit {{TARGET[:12]}}")
shas = git("rev-list", "--reverse", "--first-parent", f"{{TARGET}}..HEAD").split()
if len(shas) != len(members):
    fail(f"{{len(shas)}} commits on {{TARGET[:12]}} for {{len(members)}} recorded members")
for member, sha in zip(members, shas):
    where = f"position {{member['position']}}, commit {{sha[:12]}}"
    parents, subject, message = git("show", "-s", "--format=%P%x00%s%x00%B", sha).split("\\x00", 2)
    if len(parents.split()) != 1:
        fail(f"{{where}}: a merge commit; the chain must be linear")
    found = trailers(message)
    expected_pr = f"{{REPOSITORY}}#{{member['pr']}}"
    if found.get("Source-PR") != [expected_pr]:
        fail(f"{{where}}: expected one `Source-PR: {{expected_pr}}` trailer (recorded order), found {{found.get('Source-PR')}}")
    if found.get("Accepted-Head") != [member["accepted_head"]]:
        fail(f"{{where}}: expected one `Accepted-Head: {{member['accepted_head']}}` trailer, found {{found.get('Accepted-Head')}}")
    original = git("show", "-s", "--format=%B", member["commit"])
    suffix = f" (#{{member['pr']}})"
    if original.split("\\n", 1)[0].endswith(suffix) and not subject.endswith(suffix):
        fail(f"{{where}}: the subject no longer ends in `{{suffix}}`")
    if not message.rstrip().startswith(original.rstrip()):
        fail(f"{{where}}: the original message of {{member['commit'][:12]}} is not a prefix of the new one")
print("ok")
'''


def check_script(target: str, repository: str) -> str:
    return CHECK_SCRIPT.format(target=target, repository=repository)


STEP_SCRIPT = """#!/usr/bin/env bash
# Step the replay of this batch onto TARGET. Written by the queue's repair worker.
# Starts the rebase when none is in progress, else continues it. Members already on main stay as
# empty commits (an identical patch is kept by --reapply-cherry-picks; a conflict in one of them
# takes main's version). Stops at the first real conflict, naming the commit and the files: resolve
# it, `git add` the files and run this script again.
set -u
TARGET={target}
BASE={base}
here=$(cd "$(dirname "$0")" && pwd)
cd "$here/.." || exit 1
landed=$(python3 -c 'import json,sys;[print(r["pr"]) for r in map(json.loads, open(sys.argv[1])) if r.get("landed_on_main")]' "$here/members.jsonl")
in_progress() {{ [ -d "$(git rev-parse --git-path rebase-merge)" ]; }}
if ! in_progress; then
  if git merge-base --is-ancestor "$TARGET" HEAD; then echo "HEAD is already on ${{TARGET:0:12}}; nothing to replay (run check)"; exit 0; fi
  out=$(git rebase {options} --onto "$TARGET" "$BASE" 2>&1) || in_progress || {{ echo "$out"; exit 1; }}
fi
while in_progress; do
  sha=$(git rev-parse --verify -q REBASE_HEAD) || break
  pr=$(git show -s --format=%B "$sha" | sed -n 's/^Source-PR: .*#\\([0-9][0-9]*\\).*$/\\1/p' | head -1)
  conflicted=$(git diff --name-only --diff-filter=U)
  if [ -n "$conflicted" ] && ! grep -qx -- "${{pr:-none}}" <<<"$landed"; then
    printf 'stopped at PR #%s, commit %s: resolve the conflict in\n%s\n' "$pr" "${{sha:0:12}}" "$conflicted"; exit 1
  fi
  for path in $conflicted; do  # already on main: keep main's version
    if git cat-file -e "HEAD:$path" 2>/dev/null; then git checkout HEAD -- "$path"; else git rm -q --cached -- "$path" && rm -f "$path"; fi
  done
  # Commit the resolution with the member's own message and author, kept even when it is now empty.
  git commit -q --allow-empty -C REBASE_HEAD || exit 1
  out=$(GIT_EDITOR=true git rebase --continue 2>&1) || grep -q CONFLICT <<<"$out" || {{ echo "$out"; exit 1; }}
done
echo "rebase finished: $(git rev-list --count "$TARGET..HEAD") commits on ${{TARGET:0:12}}, HEAD $(git rev-parse --short=12 HEAD)"
"""


def step_script(target: str, base: str, options: str) -> str:
    return STEP_SCRIPT.format(target=target, base=base, options=options)


def write_listings(
    context: PromptContext, chain: list[ChainCommit], landed: dict[int, str | None] | None = None
) -> str:
    """Write the member listings, the check script, the build script and the step script beside
    ``context.listings_path``; the display path of the listings for the prompt."""
    assert context.listings_path is not None
    directory = context.listings_path.parent
    directory.mkdir(parents=True, exist_ok=True)
    context.listings_path.write_text(
        "".join(json.dumps(record, sort_keys=False) + "\n" for record in member_records(context, chain, landed))
    )
    check = directory / CHECK_FILE
    check.write_text(check_script(context.base, context.repository))
    check.chmod(0o755)
    repair_tools.write_build_script(directory)
    if context.kind == "rebase_conflict" and (context.conflict or {}).get("previous_base"):
        step = directory / STEP_FILE
        step.write_text(step_script(context.base, str(context.conflict["previous_base"]), context.rebase_options))
        step.chmod(0o755)
    return (
        f"{LISTINGS_DIR}/{LISTINGS_FILE}"
        if context.listings_path.parts[-2:] == (LISTINGS_DIR, LISTINGS_FILE)
        else str(context.listings_path)
    )


def finding_paths(findings: dict) -> list[str]:
    """Every file the diagnostics name, in their order, without repeats."""
    paths: list[str] = []
    for error in findings.get("compiler_errors") or []:
        paths.append(error["path"])
    paths += [module_to_path(m) for m in findings.get("failed_modules") or []]
    for duplicate in findings.get("duplicate_declarations") or []:
        paths += [module_to_path(m) for m in duplicate["modules"]]
    for collision in findings.get("import_collisions") or []:
        paths += [module_to_path(collision["module"]), module_to_path(collision["other_module"])]
    for finding in (findings.get("lint") or {}).get("findings") or []:
        path = finding.get("path") or (
            declaration_module(finding["declaration"]) if finding.get("declaration") else None
        )
        if path:
            paths.append(path)
    for finding in findings.get("other") or []:
        if finding.get("module"):
            paths.append(finding["module"])
    return list(dict.fromkeys(p for p in paths if p))


def _owner_text(owners: list[int], chain: list[ChainCommit]) -> str:
    """Who in the batch modifies a file, with the commit to edit: ``PR #N's commit `sha` modifies this
    file``, or that no commit of the batch does."""
    by_pr = {c.pr: c for c in chain if c.pr is not None}
    if not owners:
        return "no commit of this batch modifies this file"
    named = [f"PR #{p}'s commit `{by_pr[p].sha[:12]}`" if p in by_pr else f"PR #{p}'s commit" for p in owners]
    if len(named) == 1:
        return f"{named[0]} modifies this file"
    return " and ".join(named) + " modify this file"


def _inline_listing(context: PromptContext, chain: list[ChainCommit], landed: dict[int, str | None]) -> list[str]:
    """The member listings in the prompt itself, for a prompt composed without a worktree to write
    the listings file into (the experiment's prompt preview)."""
    lines = [
        "",
        "## Constituent commits (oldest first)",
        "",
        "Each line: commit, PR, title, recorded accepted head"
        + (", state" if conflict_position(context, chain) is not None else "")
        + ".",
        "",
    ]
    for record in member_records(context, chain, landed):
        line = f"- `{record['commit'][:12]}` #{record['pr']}: {record['title']} (`Accepted-Head: {record['accepted_head']}`"
        if record.get("provenance"):
            line += f", {record['provenance']}"
        if record.get("state"):
            line += f", {record['state']}"
        lines.append(line + ")")
    return lines


def _hours_text(seconds: int) -> str:
    words = {
        1: "one",
        2: "two",
        3: "three",
        4: "four",
        5: "five",
        6: "six",
        8: "eight",
        12: "twelve",
        24: "twenty-four",
    }
    if seconds % 3600 == 0 and seconds // 3600 in words:
        return f"{words[seconds // 3600]}-hour"
    if seconds % 60 == 0:
        return f"{seconds // 60}-minute"
    return f"{seconds}-second"


def _verification_section(
    context: PromptContext, findings: dict, base_short: str, check_command: str | None
) -> list[str]:
    conflict = context.conflict if context.kind == "rebase_conflict" else None
    lines = ["", "## Verifying locally", ""]
    if check_command:
        lines += [
            f"`{check_command}` verifies the chain: linear on `{base_short}`, exactly one commit per `Source-PR` trailer "
            "in the recorded order with the trailers and the ` (#N)` subjects kept, every original message preserved as "
            "a prefix of the new one, and a clean working tree. It prints the first problem, or `ok`. Run it before you "
            "finish.",
            "",
        ]
    scoped = (
        [
            "",
            f"Check a fix with `{LISTINGS_DIR}/{BUILD_FILE} <Module.Name>` first (add `--dependents` to include the "
            "modules that import it): it builds only those modules and waits for any other build in this checkout "
            "to finish. Run the whole-library `lake build --iofail` only as the final verdict.",
        ]
        if check_command
        else []
    )
    limit = _hours_text(context.shell_limit)
    foreground = (
        f"Long builds are fine to run in the foreground: shell commands have a {limit} limit, and if one times out, "
        "rerun the same command in the same checkout, completed modules are kept."
    )
    if context.artifacts_cold:
        modules = [
            m
            for m in (path_to_module(p) for p in [*((conflict or {}).get("paths") or []), *finding_paths(findings)])
            if m
        ]
        modules = list(dict.fromkeys(modules))
        first = (
            " Build only the modules you touched first: " + " and ".join(f"`lake build {m}`" for m in modules[:4]) + "."
            if modules
            else " Build only the modules you touched first (`lake build <Module.Name>`)."
        )
        lines += [
            "The build is cold: the worker found no warm artifacts"
            + (f" ({context.artifacts_reason})" if context.artifacts_reason else "")
            + ", so nothing of Mathlib or of this batch is built in `.lake`, and a whole-library `lake build --iofail` "
            "starts from nothing and may take hours. Do not start with it."
            + first
            + " Judge each by its exit status; the queue's CI builds the whole library once you return. Run a full "
            "`lake build --iofail` only after the rebase is finished and only if time allows. "
            + foreground
            + " Do not run `lake exe cache get` or `lake cache get` (the worker already tried); do not use "
            "`--fail-fast`.",
            *scoped,
        ]
    else:
        lines += [
            "Artifacts are warm: Mathlib and every module of this batch that compiled are already built in `.lake`, so "
            "`lake build --iofail` (whole library, incremental, as CI runs it) rebuilds only the failing modules and their "
            "dependents. Run it and judge it by its exit status. "
            + foreground
            + " Do not run `lake exe cache get` or `lake cache get`; do not use `--fail-fast`.",
            *scoped,
        ]
    lines += [
        "",
        HARD_RULES,
        "",
        "Work synchronously to completion in this turn; nothing resumes you. Finish with a short summary of what you "
        "changed in which PR's commit and the exit status of the final `lake build"
        + (" <Module.Name>` commands" if context.artifacts_cold else " --iofail`")
        + ".",
    ]
    return lines


def _positions(first: int, last: int) -> str:
    return f"position {first}" if first == last else f"positions {first} to {last}"


def _rebase_sections(
    context: PromptContext, chain: list[ChainCommit], clone: Path, base_short: str, where: str | None
) -> tuple[list[str], list[str]]:
    """What happened and the conflict evidence of a rebase conflict, naming every git ref once."""
    conflict = context.conflict or {}
    previous = (conflict.get("previous_base") or (chain[0].parent if chain else "") or "")[:12]
    stop = conflict_position(context, chain)
    paths = conflict.get("paths") or []
    member = conflict.get("member")
    number = chain[stop].pr if stop is not None else None
    commit_text = f"PR #{number}'s commit `{chain[stop].sha[:12]}`" if stop is not None else f"{member or 'one member'}"
    in_paths = " with conflicts in " + ", ".join(f"`{p}`" for p in paths) if paths else " with conflicts"
    before = stop if stop is not None else None
    prefix = (conflict.get("clean_prefix_tip") or "")[:12]
    if before:
        cleanly = (
            f" The {before} commit{'s' if before != 1 else ''} before it replayed cleanly"
            + (f" and {'are' if before != 1 else 'is'} at commit `{prefix}`" if prefix else "")
            + "."
        )
    elif before == 0:
        cleanly = " It is the first commit of the batch, so nothing replayed before it."
    else:
        cleanly = f" The commits before it replayed cleanly and are at commit `{prefix}`." if prefix else ""
    happened = [
        "## What happened",
        "",
        f"The queue replayed this batch of {len(chain)} squash commits, one per pull request, from its base `{previous}` "
        f"onto the current `main` commit `{base_short}` with git. The replay stopped at {commit_text}{in_paths}.{cleanly} "
        f"Your job is to finish that replay so the whole batch sits on `{base_short}`."
        + (f" {context.origin}" if context.origin else ""),
        "",
        f"You are in a private, detached Git worktree of {context.repository}; HEAD is the batch's tip on `{previous}`, "
        "as it was before the replay.",
    ]
    evidence = [
        "",
        "## Conflict evidence",
        "",
        f"- base, the batch's previous base: `{previous}`",
        f"- target, the current `main`: `{base_short}`",
    ]
    if stop is not None:
        title = member_records(context, chain)[stop]["title"]
        evidence.append(
            f"- conflicting commit: `{chain[stop].sha[:12]}` (PR #{number}, {_positions(stop + 1, stop + 1)} of {len(chain)}: {title})"
        )
        if stop:
            evidence.append(
                f"- clean prefix: `{prefix}` ({_positions(1, stop)} replayed onto the target)"
                if prefix
                else f"- clean prefix: {_positions(1, stop)} replayed onto the target"
            )
    elif member:
        evidence.append(f"- conflicting member: {member}" + (f"; clean prefix `{prefix}`" if prefix else ""))
    for path in paths:
        owners = owners_of(clone, chain, path)
        evidence.append(f"- `{path}`: {_owner_text(owners, chain)}, and `main` has changed it since the batch's base")
    pending = len(chain) - stop - 1 if stop is not None else len(conflict.get("pending") or [])
    if pending and stop is not None:
        evidence.append(
            f"- not yet replayed: {pending} commit{'s' if pending != 1 else ''} after it ({_positions(stop + 2, len(chain))})"
        )
    elif pending:
        evidence.append(f"- not yet replayed: {pending} commit{'s' if pending != 1 else ''} after it")
    if where:
        evidence.append(
            f"- every member with its `position`, `commit`, `pr`, `title`, `accepted_head`, `provenance` and `state` "
            f"(`replayed`, `conflicting` or `pending`): `{where}`, one JSON object per line, in chain order; `grep` it "
            "for a PR number or commit (ignored by Git)"
        )
    return happened, evidence


def _landed_section(
    context: PromptContext, chain: list[ChainCommit], landed: dict[int, str | None], where: str | None
) -> list[str]:
    """The members whose PR is already on ``main``: one line each up to ``LANDED_SHOWN``, the rest by
    count, with what to do about them."""
    if not landed:
        return []
    records = [r for r in member_records(context, chain, landed) if r["pr"] in landed]
    lines = [
        "",
        "## Already on main",
        "",
        f"{len(records)} of the {len(chain)} members' PRs have landed on `main` outside the queue since the batch's "
        "base. Each becomes an empty commit: its patch is already in the base (`--reapply-cherry-picks` keeps an "
        "identical patch as an empty commit rather than dropping it), and a conflict in one of them is resolved by "
        "taking `main`'s version."
        + (f" They are marked `landed_on_main` (the landing commit) in `{where}`." if where else ""),
        "",
    ]
    for record in records[:LANDED_SHOWN]:
        landing = f", landed as `{record['landed_on_main'][:12]}`" if record["landed_on_main"] != "landed" else ""
        lines.append(f"- PR #{record['pr']}: {record['title']}{landing}")
    if len(records) > LANDED_SHOWN:
        lines.append(f"- and {len(records) - LANDED_SHOWN} more" + (f", marked in `{where}`" if where else ""))
    return lines


def _result_section(base_short: str) -> list[str]:
    return [
        "",
        "## The result",
        "",
        f"The result is the commits in this worktree: a linear chain on `{base_short}` with one commit per PR in the same "
        "order as now, each squash commit keeping its `Source-PR:` and `Accepted-Head:` trailers and its subject ending "
        "in ` (#N)`. The queue reads the chain from here when you finish, validates it and runs CI itself, so there is "
        "nothing to push, publish or report anywhere else, and `gh` is not needed. Leave HEAD (detached) at the chain "
        "tip with a clean working tree.",
        "",
        f"Do not introduce changes to {PROTECTED_PATHS}; a constituent commit that itself modifies such a file keeps "
        "its change.",
    ]


def _autosquash(base_short: str) -> str:
    return f"`GIT_EDITOR=true GIT_SEQUENCE_EDITOR=: git rebase -i --autosquash --keep-empty --empty=keep {base_short}`"


def _editing_section(context: PromptContext, chain: list[ChainCommit], base_short: str, where: str | None) -> list[str]:
    conflict = context.conflict if context.kind == "rebase_conflict" else None
    if conflict:
        previous = (conflict.get("previous_base") or (chain[0].parent if chain else "") or "")[:12]
        stop = conflict_position(context, chain)
        at = f"`{chain[stop].sha[:12]}`" if stop is not None else (conflict.get("member") or "the conflicting member")
        return [
            "",
            "## The rebase",
            "",
            f"Run `git rebase {context.rebase_options} --onto {base_short} {previous}`. It replays the chain commit "
            f"by commit onto `{base_short}` and stops at {at} with the conflict above, and at any later commit that "
            "conflicts. At each stop, resolve the conflict inside that commit: edit the conflicting files, `git add` "
            "them, `git commit --allow-empty -C REBASE_HEAD` (the commit's own message and author, kept even when the "
            "resolution leaves it empty), then `GIT_EDITOR=true git rebase --continue`. A commit whose change is "
            f"already on `{base_short}` becomes an empty commit, never a dropped one; do not `git rebase --skip`."
            + (
                f" `bash {LISTINGS_DIR}/{STEP_FILE}` does this stepping for you: it starts the rebase if none is in "
                "progress, keeps members already on `main` as empty commits (taking `main`'s version of any conflict "
                "in them) and stops at the first real conflict, naming the commit and the files; resolve it, `git add` "
                "the files and run it again."
                if where
                else ""
            ),
            "",
            "After the rebase has finished, append your explanation to the message of each commit whose content you "
            'changed: `git commit --allow-empty --squash=<sha> -m "<explanation>"`, then '
            + _autosquash(base_short)
            + ". This keeps the commit's message and author and adds the explanation after the trailers. To change "
            "the content of a commit that has already been replayed, make the edit, `git add` it, "
            "`git commit --fixup=<sha>`, and run the same autosquash rebase.",
        ]
    source = f" (the owning commit's SHA is in the evidence above{' and in `' + where + '`' if where else ''})"
    return [
        "",
        "## Editing constituent commits",
        "",
        "Edit the commit that owns the change rather than adding a repair commit on top: make the fix in the working "
        f"tree, `git add` it, `git commit --fixup=<sha>`{source}, and append your explanation to that commit's message "
        'with `git commit --allow-empty --squash=<sha> -m "<explanation>"`; then '
        + _autosquash(base_short)
        + " folds them into the chain, keeping each commit's message and author and adding the explanation after the "
        "trailers. A contribution reduced to nothing stays as an empty commit, never a dropped one.",
    ]


def compose_prompt(context: PromptContext, findings: dict, chain: list[ChainCommit], clone: Path) -> str:
    """The plan's instruction, then what happened, the evidence with git refs, the result the queue
    expects, the editing instruction and the local verification.

    With a ``listings_path`` the member listings go to ``.tauceti-repair/members.jsonl`` in the
    worktree and the check script beside it (``write_listings``); without one (the experiment's
    prompt preview) the listings are inline. ``artifacts_cold`` changes the verification advice from
    an incremental whole-library build to building the touched modules first.
    """
    base_short = context.base[:12]
    lines = [context.instruction or AGENT_INSTRUCTION, ""]
    conflict = context.conflict if context.kind == "rebase_conflict" else None
    landed = landed_on_main(context, chain, clone)
    where = write_listings(context, chain, landed) if context.listings_path is not None else None
    check_command = f"python3 {LISTINGS_DIR}/{CHECK_FILE}" if where else None
    if conflict:
        happened, evidence = _rebase_sections(context, chain, clone, base_short, where)
        lines += happened + evidence
    else:
        lines += [
            "## What happened",
            "",
            f"The queue built this batch of {len(chain)} squash commits, one per pull request, on the `main` commit "
            f"`{base_short}` with CI (`lake build --iofail`, as the staging build runs it) and the build failed."
            + (f" {context.origin}" if context.origin else "")
            + f" Your job is to make the whole batch build on `{base_short}`.",
            "",
            f"You are in a private, detached Git worktree of {context.repository}; HEAD is the batch's tip on "
            f"`{base_short}`.",
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
            lines.append(
                f"- `{error['path']}:{error.get('line', 0)}:{error.get('column', 0)}` ({_owner_text(owners, chain)})"
            )
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
            lines.append(
                f"- `{duplicate['declaration']}` is declared in both "
                + " and ".join(f"`{m}` ({_owner_text(owners_of(clone, chain, m), chain)})" for m in modules)
            )
        lines.append("")
    if findings["import_collisions"]:
        lines.append("Audit findings (import collisions):")
        lines.append("")
        for collision in findings["import_collisions"]:
            module, other = module_to_path(collision["module"]), module_to_path(collision["other_module"])
            lines.append(
                f"- importing `{module}` ({_owner_text(owners_of(clone, chain, module), chain)}) "
                f"failed: the environment already contains `{collision['declaration']}` from `{other}` "
                f"({_owner_text(owners_of(clone, chain, other), chain)})"
            )
        lines.append("")
    if findings["lint"]["findings"] or findings["lint"]["report"]:
        lines.append(
            "Environment lint findings (new violations against the grandfathered baseline; CI runs this lint):"
        )
        lines.append("")
        for finding in findings["lint"]["findings"]:
            path = finding.get("path") or (
                declaration_module(finding["declaration"]) if finding.get("declaration") else None
            )
            owners = owners_of(clone, chain, path) if path else []
            where_text = f" in `{path}` ({_owner_text(owners, chain)})" if path else ""
            lines.append(f"- linter `{finding.get('linter')}`: `{finding.get('declaration')}`{where_text}")
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
            where_text = f" in `{finding['module']}` ({_owner_text(owners, chain)})" if finding.get("module") else ""
            lines.append(f"- {finding['kind']}{where_text}: {finding['message']}")
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
    if not conflict and where:
        lines.append(
            f"Every member with its `position`, `commit`, `pr`, `title`, `accepted_head` and `provenance` is in "
            f"`{where}`, one JSON object per line, in chain order; `grep` it for a PR number or commit (ignored by Git)."
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
    if where is None:
        lines += _inline_listing(context, chain, landed)
    lines += _landed_section(context, chain, landed, where)
    if context.repair_context is not None and where:
        lines += context.repair_context.prompt_lines()
    lines += _result_section(base_short)
    lines += _editing_section(context, chain, base_short, where)
    lines += _verification_section(context, findings, base_short, check_command)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines))


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


def codex_profile():
    from tauceti_worker.agents import resolve_authoring_profile

    return resolve_authoring_profile("codex")


# The repair agent's provider, as the task's provider chain names it: the Claude subscription, the Codex
# subscription, or Claude billed to the API through ``ANTHROPIC_API_KEY``.
PROVIDERS = ("claude", "codex", "api")


def prepare_claude_config(root: Path, *, credentials: bool = True) -> Path:
    """An isolated Claude config dir seeded the way workers seed theirs: credentials, not personality.

    With ``credentials=False`` (the ``api`` provider) the dir is a separate one that holds no
    subscription credential at all, so ``claude`` can only authenticate with the API key in its
    environment and the subscription is never billed by accident."""
    from tauceti_worker.agents import seed_worker_claude_config
    from tauceti_worker.quota import claude_dir

    iso = root / ("claude-config" if credentials else "claude-config-api")
    iso.mkdir(parents=True, exist_ok=True)
    real = claude_dir(Path(os.environ.get("HOME", os.path.expanduser("~"))))
    seed_worker_claude_config(real, iso)
    for name in (".credentials.json",):
        src = real / name
        if credentials and src.exists():
            shutil.copy2(src, iso / name)
        elif not credentials:
            (iso / name).unlink(missing_ok=True)
    return iso


def prepare_codex_home(root: Path) -> Path:
    """An isolated ``CODEX_HOME`` holding only the operator's Codex credential (``auth.json``), copied
    from the real one (``$CODEX_HOME``, default ``~/.codex``) the way ``agents.isolate_home`` seeds a
    worker's: no host ``config.toml`` (the model and effort come from the authoring profile's flags)."""
    from tauceti_worker.quota import codex_dir

    iso = root / "codex-home"
    iso.mkdir(parents=True, exist_ok=True)
    real = codex_dir(Path(os.environ.get("HOME", os.path.expanduser("~"))))
    src = real / "auth.json"
    if src.exists():
        shutil.copy2(src, iso / "auth.json")
    return iso


def isolation_env(
    root: Path, env_lake: dict | None = None, provider: str = "claude", *, api_key: str | None = None
) -> dict:
    """Environment changes that keep a repair agent private: no GitHub credentials, no push, its
    own provider config dir with that provider's credential and no other, and the private artifact
    pool. ``provider`` is one of ``PROVIDERS``; the ``api`` provider gets ``api_key`` as
    ``ANTHROPIC_API_KEY`` and ``TAUCETI_CLAUDE_BILLING=api``, the subscription providers get neither."""
    if provider not in PROVIDERS:
        raise ValueError(f"unknown repair provider {provider!r} (one of {', '.join(PROVIDERS)})")
    changes: dict = {}
    for key in LAKE_ENV_KEYS:
        if env_lake and key in env_lake:
            changes[key] = env_lake[key]
    changes["OPENAI_API_KEY"] = None
    if provider == "api":
        changes["CLAUDE_CONFIG_DIR"] = str(prepare_claude_config(root, credentials=False))
        changes["ANTHROPIC_API_KEY"] = api_key
        changes["TAUCETI_CLAUDE_BILLING"] = "api"
    else:
        changes["ANTHROPIC_API_KEY"] = None
        changes["TAUCETI_CLAUDE_BILLING"] = None
        if provider == "codex":
            changes["CODEX_HOME"] = str(prepare_codex_home(root))
        else:
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


def agent_environment(
    prompt: str, root: Path, env_lake: dict, deadline_seconds: float
) -> tuple[list[str], dict, str | None]:
    """The worker's exact argv for a Claude host round, plus the experiment's isolation, plus the text
    for the agent's stdin: the prompt when it is too long for argv (``agents.PROMPT_ARGV_LIMIT``), else
    None because the prompt is in argv."""
    os.environ["TAUCETI_ROUND_DEADLINE"] = str(time.time() + deadline_seconds)
    from tauceti_worker.agents import host_agent_launch

    argv, env, stdin_text = host_agent_launch(prompt, claude_profile())
    for key, value in isolation_env(root, env_lake).items():
        if value is None:
            env.pop(key, None)
        else:
            env[key] = value
    return argv, env, stdin_text


def run_agent(
    worktree: Path, argv: list[str], env: dict, logdir: Path, timeout: float, stdin_text: str | None = None
) -> dict:
    """Run the agent with its stream-json transcript captured; wall time from handoff to exit.

    ``stdin_text`` is written to the agent's stdin and then closed (a prompt too long for argv); without
    it stdin is closed at once. A process that cannot be created at all (``OSError`` from ``Popen``: a
    missing executable, an argv the kernel rejects) is reported as ``"outcome": "agent_launch_failed"``
    with ``rc`` None, rather than raised, so the record names the launch as the cause.
    """
    logdir.mkdir(parents=True, exist_ok=True)
    stream = logdir / "agent-stream.jsonl"
    stderr = logdir / "agent-stderr.log"
    before = host_state(worktree)
    started = time.monotonic()
    timed_out = False
    launch_error = None
    with stream.open("wb") as out, stderr.open("wb") as err:
        stdin = subprocess.DEVNULL if stdin_text is None else subprocess.PIPE
        try:
            proc = subprocess.Popen(
                argv, cwd=worktree, env=env, stdin=stdin, stdout=out, stderr=err, start_new_session=True
            )
        except OSError as error:
            launch_error = f"{type(error).__name__}: {error}"
            err.write(f"agent process could not start: {launch_error}\n".encode())
            rc = None
        else:
            try:
                # communicate() writes the whole prompt and closes stdin; stdout/stderr are files, so
                # nothing here waits on the agent's output.
                proc.communicate(input=None if stdin_text is None else stdin_text.encode(), timeout=timeout)
                rc = proc.returncode
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
        "outcome": "agent_launch_failed" if launch_error else ("timed_out" if timed_out else "exited"),
        "launch_error": launch_error,
        "prompt_via": "argv" if stdin_text is None else "stdin",
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

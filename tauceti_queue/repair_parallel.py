"""Parallel agents within one repair task (``docs/redesign/specs/ai-merge-queue.md``, improvement 4 of
the five from the first production repair).

When a repair task's failures fall into independent groups (different owning members, disjoint files,
and no group's files imported by another group's modules as far as ``import`` lines show), the worker
runs one agent per group concurrently, up to ``RunnerConfig.parallel_agents``. Each agent works in its
own detached worktree of the chain tip (added from the task's worktree, sharing the extracted
``.lake/packages`` by symlink) with a prompt scoped to its group, and leaves ``fixup!`` and ``squash!``
commits for the owning members on top of the tip instead of rewriting the chain. The worker then
cherry-picks every group's commits onto the tip in the task's worktree, folds them with one autosquash
rebase, validates the chain with the usual checks and runs the single whole-library
``lake build --iofail``. A group whose agent failed, whose commits were not the fixups asked for, or
whose fixups do not apply is handed back to the sequential path in ``repair_task.run_task``, which runs
the usual agent on the task's worktree with the remaining findings.

Each group's agent runs in a child process of the worker (``python -m tauceti_queue.repair_parallel
run-group <job>``, ``run_group_job``) that calls ``repair_task.run_repair_agent`` exactly as the
sequential path does: the agent path applies its provider environment to the process (``os.environ``)
and keeps the last run's failure in module globals, neither of which several threads could share. The
worker chooses the eligible providers once for every group (``select_providers``) and the child takes
that list as given. The parent terminates the child when the task is cancelled; the child stops its
agent and reports ``cancelled``.

When the worker streamed the first build (``repair_stream``), the groups are taken from its complete
error list, ``.tauceti-repair/errors.jsonl``, merged with the task's diagnostics (``read_errors``,
``merge_errors``); the parallel path waits for that build to finish first, since a group is only
independent against the whole list.

Builds are serialized across the worktrees by one lock directory, the task worktree's
``.tauceti-repair/build.lock`` (``repair_tools``: ``mkdir`` atomically, the builder's pid inside,
removed when the build ends, a lock whose holder has died is cleared). Every group worktree gets
``.tauceti-repair/build``, the same scoped build script the sequential agent has, written with that
shared lock baked in, and the worker's verification build holds the lock through
``repair_tools.build_lock``. Every instruction in the group prompt is plain shell and python: the same
prompt serves whichever provider runs the agent, and nothing depends on an agent spawning helpers of
its own.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import contextlib
import dataclasses
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
import types
import typing
from pathlib import Path

from tauceti_queue import repair, repair_context, repair_sandbox, repair_stream, repair_tools
from tauceti_queue.repair import ChainCommit, PromptContext, git

DEFAULT_PARALLEL_AGENTS = 3
REPO_ROOT = Path(__file__).resolve().parent.parent
GROUP_HEADING = "## Your part of the repair"
API_KEY_ENV = "TAUCETI_QUEUE_GROUP_API_KEY"  # the api provider's key, handed to the child in its environment only
FIXUP_SUBJECT = re.compile(r"^(fixup|squash)! (.+)$")
IMPORT_LINE = re.compile(r"^\s*import\s+([A-Za-z_][\w'.]*)", re.M)
# Agent statuses that mean the provider, not the group, is the problem; with no group repaired they
# release the task under the same name (``repair_task.RELEASED_AGENT_STATUSES``).
PROVIDER_STATUSES = ("inference_unavailable", "artifacts_unavailable", "agent_launch_failed")


def parallel_agents_default() -> int:
    """``TAUCETI_QUEUE_PARALLEL_AGENTS`` (default 3); 0 or 1 disables the parallel path."""
    try:
        return max(0, int(os.environ.get("TAUCETI_QUEUE_PARALLEL_AGENTS") or DEFAULT_PARALLEL_AGENTS))
    except ValueError:
        return DEFAULT_PARALLEL_AGENTS


# ----------------------------------------------------------------------------------------------
# Grouping


@dataclasses.dataclass
class Group:
    """Failures that one agent can repair on its own: the owning members, the files their commits
    change, the failing paths among them and the findings restricted to those paths."""

    name: str
    members: list[int]
    files: list[str]
    paths: list[str]
    findings: dict
    status: str = "planned"
    worktree: str | None = None
    seconds: float | None = None
    agent: dict | None = None
    fixups: list[str] = dataclasses.field(default_factory=list)
    detail: str | None = None

    def summary(self) -> dict:
        record = dataclasses.asdict(self)
        record.pop("findings")
        return record


def _path(name: str) -> str:
    """A module name or a path, as a path (the diagnostics use both forms)."""
    return name if name.endswith(".lean") else repair.module_to_path(name)


def finding_items(findings: dict) -> list[tuple[str, str, object]]:
    """Every finding with the path it concerns: ``(path, family, entry)``, families as the findings
    structure names them. An entry naming several modules appears once per module."""
    items: list[tuple[str, str, object]] = []
    for error in findings.get("compiler_errors") or []:
        items.append((_path(error["path"]), "compiler_errors", error))
    for module in findings.get("failed_modules") or []:
        items.append((_path(module), "failed_modules", module))
    for duplicate in findings.get("duplicate_declarations") or []:
        for module in duplicate["modules"]:
            items.append((_path(module), "duplicate_declarations", duplicate))
    for collision in findings.get("import_collisions") or []:
        for module in (collision["module"], collision["other_module"]):
            items.append((_path(module), "import_collisions", collision))
    for finding in (findings.get("lint") or {}).get("findings") or []:
        path = finding.get("path") or (
            repair.declaration_module(finding["declaration"]) if finding.get("declaration") else None
        )
        items.append((_path(path) if path else "", "lint", finding))
    for finding in findings.get("other") or []:
        items.append((_path(finding["module"]) if finding.get("module") else "", "other", finding))
    return items


def restrict_findings(findings: dict, paths: set[str]) -> dict:
    """The findings structure with only the entries concerning ``paths``."""
    out = repair.empty_findings()
    out["other"] = []
    out["conflict"] = None
    for path, family, entry in finding_items(findings):
        if path not in paths:
            continue
        if family == "lint":
            if entry not in out["lint"]["findings"]:
                out["lint"]["findings"].append(entry)
        elif entry not in out[family]:
            out[family].append(entry)
    return out


def imports_of(worktree: Path, path: str) -> list[str]:
    """The module paths a Lean file's ``import`` lines name, read from the worktree (at the tip)."""
    try:
        text = (worktree / path).read_text(errors="replace")
    except OSError:
        return []
    return [repair.module_to_path(name) for name in IMPORT_LINE.findall(text)]


def plan_groups(
    clone: Path, chain: list[ChainCommit], findings: dict, worktree: Path
) -> tuple[list[Group], str | None]:
    """Split the findings into independent groups, or explain why they do not split.

    Two failing paths share a group when a member's commit changes both, when the owning members'
    changed files overlap, or when a file of one member's commit imports a file of the other's. A
    finding whose path no member's commit changes (a ripple into a module of ``main``) cannot be
    assigned, so the task stays sequential. The result has at least two groups or is empty with the
    reason.
    """
    items = finding_items(findings)
    paths = list(dict.fromkeys(path for path, _, _ in items if path))
    if any(not path for path, _, _ in items):
        return [], "a finding names no module"
    if not paths or not chain:
        return [], "no failing paths"
    by_pr = {c.pr: c for c in chain if c.pr is not None}
    owners: dict[str, list[int]] = {}
    for path in paths:
        owners[path] = repair.owners_of(clone, chain, path)
        if not owners[path]:
            return [], f"no member's commit changes {path}"
    order = [c.pr for c in chain]
    members = sorted({pr for prs in owners.values() for pr in prs}, key=order.index)
    if len(members) < 2:
        return [], "one member owns every failing module"
    files = {
        pr: git(clone, "diff-tree", "--no-commit-id", "--name-only", "-r", by_pr[pr].sha).split() for pr in members
    }
    for path, prs in owners.items():
        for pr in prs:
            if path not in files[pr]:
                files[pr].append(path)
    parent = {pr: pr for pr in members}

    def root(pr: int) -> int:
        while parent[pr] != pr:
            parent[pr] = parent[parent[pr]]
            pr = parent[pr]
        return pr

    def unite(a: int, b: int) -> None:
        parent[root(a)] = root(b)

    for prs in owners.values():
        for other in prs[1:]:
            unite(prs[0], other)
    file_owner: dict[str, int] = {}
    for pr in members:
        for path in files[pr]:
            if path in file_owner:
                unite(pr, file_owner[path])
            file_owner.setdefault(path, pr)
    for pr in members:
        for path in files[pr]:
            for imported in imports_of(worktree, path):
                if imported in file_owner and root(file_owner[imported]) != root(pr):
                    unite(pr, file_owner[imported])
    components: dict[int, list[int]] = {}
    for pr in members:
        components.setdefault(root(pr), []).append(pr)
    if len(components) < 2:
        return [], "the failing modules depend on each other"
    groups = []
    for index, prs in enumerate(components.values(), 1):
        group_files = sorted({path for pr in prs for path in files[pr]})
        group_paths = [path for path in paths if set(owners[path]) & set(prs)]
        groups.append(
            Group(
                name=f"group-{index}",
                members=prs,
                files=group_files,
                paths=group_paths,
                findings=restrict_findings(findings, set(group_paths)),
            )
        )
    return groups, None


def read_errors(directory: Path) -> list[dict] | None:
    """The compiler errors of the streaming build's ``errors.jsonl`` in ``directory`` (the worktree's
    ``.tauceti-repair``), without its event lines; None when there is no such file."""
    path = directory / repair_stream.ERRORS_FILE
    if not path.exists():
        return None
    errors = []
    for line in path.read_text(errors="replace").splitlines():
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(entry, dict) and entry.get("file") and not entry.get("event"):
            errors.append(entry)
    return errors


def merge_errors(findings: dict, errors: list[dict]) -> dict:
    """The task's findings with the streaming build's errors added as compiler errors (a location the
    diagnostics already report is not repeated) and their modules as failed modules."""
    merged = json.loads(json.dumps(findings))
    located = {(e["path"], int(e.get("line") or 0), int(e.get("column") or 0)) for e in merged["compiler_errors"]}
    for error in errors:
        path = str(error["file"])
        key = (path, int(error.get("line") or 0), int(error.get("column") or 0))
        if key in located:
            continue
        located.add(key)
        merged["compiler_errors"].append(
            {"path": path, "line": key[1], "column": key[2], "message": str(error.get("message") or "")}
        )
        if path not in merged["failed_modules"]:
            merged["failed_modules"].append(path)
    return merged


# ----------------------------------------------------------------------------------------------
# The group prompt


def lock_path(worktree: Path) -> Path:
    """The task worktree's build lock, shared by every group worktree's build script."""
    return worktree / repair_tools.TOOLS_DIR / repair_tools.LOCK_DIR


def group_prompt(context: PromptContext, group: Group, chain: list[ChainCommit], clone: Path, tip: str) -> str:
    """The scoped prompt of one group: the instruction, what happened, this group's evidence and the
    members it may edit, the fixup-commit protocol and the lock-aware build script. Plain shell and
    python throughout, so the same text serves any provider."""
    base_short = context.base[:12]
    by_pr = {c.pr: c for c in chain if c.pr is not None}
    findings = group.findings
    tools = repair.LISTINGS_DIR
    lines = [
        context.instruction or repair.AGENT_INSTRUCTION,
        "",
        "## What happened",
        "",
        f"The queue built this batch of {len(chain)} squash commits, one per pull request, on the `main` commit "
        f"`{base_short}` with CI (`lake build --iofail`, as the staging build runs it) and the build failed. The "
        "failures split into independent groups of modules; this run repairs one group while other runs repair the "
        "others at the same time, each in its own worktree. The worker folds every group's fixes into the chain and "
        "runs the whole-library build itself afterwards.",
        "",
        f"You are in a private, detached Git worktree of {context.repository}; HEAD is the batch's tip `{tip[:12]}` on "
        f"`{base_short}`.",
        "",
        GROUP_HEADING,
        "",
        "Members whose commits you may edit:",
        "",
    ]
    for pr in group.members:
        commit = by_pr.get(pr)
        title = re.sub(r"\s*\(#\d+\)\s*$", "", commit.subject) if commit else ""
        lines.append(f"- PR #{pr}: commit `{commit.sha[:12] if commit else '?'}` {title}".rstrip())
    lines += ["", "Files you may edit: " + ", ".join(f"`{path}`" for path in group.files) + ".", ""]
    if findings["compiler_errors"]:
        lines += ["Compiler errors:", ""]
        for error in findings["compiler_errors"]:
            owners = repair.owners_of(clone, chain, _path(error["path"]))
            lines.append(
                f"- `{error['path']}:{error.get('line', 0)}:{error.get('column', 0)}` ({repair._owner_text(owners, chain)})"
            )
            lines += [f"      {text}" for text in str(error.get("message", "")).splitlines()]
        lines.append("")
    if findings["failed_modules"] and not findings["compiler_errors"]:
        lines += ["Modules CI failed to build: " + ", ".join(f"`{m}`" for m in findings["failed_modules"]) + ".", ""]
    if findings["duplicate_declarations"]:
        lines += ["Audit findings (duplicate declarations; the audit rejects a name declared twice):", ""]
        for duplicate in findings["duplicate_declarations"]:
            lines.append(
                f"- `{duplicate['declaration']}` is declared in both "
                + " and ".join(f"`{_path(m)}`" for m in duplicate["modules"])
            )
        lines.append("")
    if findings["import_collisions"]:
        lines += ["Audit findings (import collisions):", ""]
        for collision in findings["import_collisions"]:
            lines.append(
                f"- importing `{_path(collision['module'])}` failed: the environment already contains "
                f"`{collision['declaration']}` from `{_path(collision['other_module'])}`"
            )
        lines.append("")
    if findings["lint"]["findings"]:
        lines += ["Environment lint findings (new violations against the grandfathered baseline):", ""]
        for finding in findings["lint"]["findings"]:
            where = f" in `{finding['path']}`" if finding.get("path") else ""
            lines.append(f"- linter `{finding.get('linter')}`: `{finding.get('declaration')}`{where}")
        lines.append("")
    if findings.get("other"):
        lines += ["Other CI findings:", ""]
        for finding in findings["other"]:
            where = f" in `{finding['module']}`" if finding.get("module") else ""
            lines.append(f"- {finding['kind']}{where}: {finding['message']}")
        lines.append("")
    lines += [
        "Edit only the files listed above. Every other failing module belongs to another run; a fix that would need "
        "a change elsewhere (another member's file, a file of `main`) is out of scope here: leave that module alone "
        "and say so in your summary.",
        "",
        f"Every member with its `position`, `commit`, `pr`, `title`, `accepted_head` and `provenance` is in "
        f"`{tools}/{repair.LISTINGS_FILE}`, one JSON object per line, in chain order (ignored by Git).",
    ]
    ownership = getattr(context, "repair_context", None)
    if ownership is not None and hasattr(ownership, "prompt_lines"):
        lines += ownership.prompt_lines()
    modules = " ".join(m for m in (repair.path_to_module(p) for p in group.paths) if m)
    lines += [
        "",
        "## The result",
        "",
        "Do not rebase, reorder, squash or amend the chain: the worker does the folding. For each fix, make the edit "
        "in the working tree, `git add` it and `git commit --fixup=<sha>` naming the owning member's commit from the "
        "list above; then append a very concise explanation of what changed and why with "
        '`git commit --allow-empty --squash=<sha> -m "<explanation>"` on the same commit. Leave the fixup and squash '
        f"commits on top of `{tip[:12]}` (HEAD detached, working tree clean) and stop. The worker cherry-picks them "
        "onto the chain, folds them into the owning commits, validates the chain and runs CI itself, so there is "
        "nothing to push, publish or report anywhere else, and `gh` is not needed.",
        "",
        f"Do not introduce changes to {repair.PROTECTED_PATHS}.",
        "",
        "## Verifying locally",
        "",
        "Artifacts are warm: Mathlib and every module of this batch that compiled are already built and are restored "
        f"from the worker's artifact pool. Build the modules you touched with `python3 {tools}/{repair_tools.BUILD_FILE} "
        f"{modules}` (it runs `lake build --iofail` on the named modules, waiting for the other runs' builds so that "
        "only one `lake build` is active at a time; add `--dependents` when a declaration changed) and judge it by "
        "its exit status. Do not run `lake build` directly, and do not run `lake exe cache get` or `lake cache get`. "
        "The whole-library build is the worker's; do not run it here. "
        f"Shell commands have a {repair._hours_text(context.shell_limit)} limit, and if one times out, rerun the same "
        "command in the same checkout, completed modules are kept.",
        "",
        repair.HARD_RULES,
        "",
        "Work synchronously to completion in this turn; nothing resumes you. Finish with a short summary of what you "
        "changed in which PR's commit and the exit status of the final build command.",
    ]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines))


# ----------------------------------------------------------------------------------------------
# The child process: one group's agent


class GroupState:
    """What ``repair_task.run_repair_agent`` asks of a task state, for one group: a checkpoint file of
    its own under the task's state directory and the session file the host agent writes."""

    def __init__(self, directory: Path):
        self.directory = directory
        self.path = directory / "group.json"
        self.session_path = directory / "session.json"
        self.record: dict = {"history": []}

    def write(self, state: str, **fields) -> None:
        from tauceti_worker.runtime_status import atomic_json

        self.record.update(fields)
        self.record["state"] = state
        self.record["updated_at"] = time.time()
        self.record["history"].append({"state": state, "at": self.record["updated_at"]})
        atomic_json(self.path, self.record)

    def session(self) -> str | None:
        from tauceti_worker.runtime_status import read_json

        value = read_json(self.session_path).get("session")
        return str(value) if value else None


def config_to_json(cfg) -> dict:
    """A ``RunnerConfig`` for the job file: paths and tuples as JSON, nested settings as objects, the
    secrets left out (the token is not needed by the agent; the api key travels in the child's
    environment), a profile object as the default name."""
    record = {}
    for field in dataclasses.fields(cfg):
        value = getattr(cfg, field.name)
        if field.name in ("token", "api_key"):
            value = None
        elif field.name == "profile":
            value = value if isinstance(value, str) else "claude"
        record[field.name] = _to_json(value)
    return record


def _to_json(value):
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {f.name: _to_json(getattr(value, f.name)) for f in dataclasses.fields(value)}
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (tuple, list)):
        return [_to_json(v) for v in value]
    return value


def config_from_json(record: dict, cls):
    """The inverse of ``config_to_json``, typing paths, tuples and nested dataclasses back from the
    class's annotations."""
    hints = typing.get_type_hints(cls)
    values = {}
    for field in dataclasses.fields(cls):
        if field.name in record:
            values[field.name] = _from_json(record[field.name], hints.get(field.name))
    return cls(**values)


def _from_json(value, hint):
    if value is None or hint is None:
        return value
    origin = typing.get_origin(hint)
    options = list(typing.get_args(hint)) if origin in (typing.Union, types.UnionType) else [hint]
    for option in options:
        if option is Path and isinstance(value, str):
            return Path(value)
        if dataclasses.is_dataclass(option) and isinstance(value, dict):
            return config_from_json(value, option)
        if typing.get_origin(option) is tuple and isinstance(value, list):
            return tuple(value)
    return value


def run_group_job(job_path: Path) -> int:
    """The child's entry: run one group's agent as ``repair_task.run_repair_agent`` would in the worker,
    with the providers the worker chose, and write its record to the job's ``result`` file. SIGTERM
    from the parent cancels the agent, which is then reported as ``cancelled``."""
    from tauceti_queue import repair_task

    job = json.loads(job_path.read_text())
    cfg = config_from_json(job["cfg"], repair_task.RunnerConfig)
    cfg.api_key = os.environ.pop(API_KEY_ENV, None) or None
    task = repair_task.Task.from_json(job["task"])
    cancel = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: cancel.set())
    state = GroupState(Path(job["state_dir"]))
    try:
        result = repair_task.run_repair_agent(
            cfg,
            task,
            Path(job["worktree"]),
            Path(job["prompt"]).read_text(),
            Path(job["logdir"]),
            cancel,
            state,
            job["env_lake"],
            label=job["label"],
            preselected=job["providers"],
        )
    except Exception as error:  # the group fails; the sequential path takes it
        result = {"status": "failed", "reason": f"{type(error).__name__}: {error}", "attempts": []}
    result_path = Path(job["result"])
    tmp = result_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(result, default=str))
    os.replace(tmp, result_path)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m tauceti_queue.repair_parallel", description=__doc__.split("\n\n")[0]
    )
    commands = parser.add_subparsers(dest="command", required=True)
    group = commands.add_parser("run-group", help="run one group's agent from its job file (the worker's child)")
    group.add_argument("job", type=Path)
    args = parser.parse_args(argv)
    return run_group_job(args.job)


# ----------------------------------------------------------------------------------------------
# Collecting and folding the groups' commits


def collect_fixups(group_worktree: Path, tip: str, group: Group) -> tuple[list[str], str | None]:
    """The commits a group agent left on top of the tip, oldest first, when they are what was asked:
    ``fixup!``/``squash!`` commits, the fixups touching only the group's files, and a clean tree.
    Otherwise the reason they are not usable."""
    if git(group_worktree, "status", "--porcelain", "--untracked-files=no").strip():
        return [], "the working tree was left with uncommitted changes"
    shas = git(group_worktree, "rev-list", "--reverse", "--first-parent", f"{tip}..HEAD").split()
    if not shas:
        return [], "no commits were left on top of the tip"
    allowed = set(group.files)
    for sha in shas:
        subject = git(group_worktree, "log", "-1", "--format=%s", sha).strip()
        match = FIXUP_SUBJECT.match(subject)
        if not match:
            return [], f"commit {sha[:12]} is not a fixup! or squash! commit ({subject[:60]})"
        if match.group(1) == "fixup":
            touched = set(git(group_worktree, "diff-tree", "--no-commit-id", "--name-only", "-r", sha).split())
            outside = sorted(touched - allowed)
            if outside:
                return [], f"fixup {sha[:12]} changes files outside the group: {', '.join(outside[:5])}"
    return shas, None


def fold_fixups(worktree: Path, base: str, tip: str, groups: list[Group]) -> dict:
    """Cherry-pick every repaired group's commits onto the tip in the task's worktree and fold them
    into the owning members with one autosquash rebase. A group whose commits do not apply is marked
    ``fixups_not_applied`` and left for the sequential path; a rebase that fails leaves the worktree
    at the tip with every group handed back."""
    env = {**os.environ, "GIT_EDITOR": "true", "GIT_SEQUENCE_EDITOR": ":"}
    git(worktree, "checkout", "--quiet", "--force", "--detach", tip)
    record: dict = {"cherry_picked": 0, "applied": [], "not_applied": []}
    for group in groups:
        if group.status != "repaired":
            continue
        before = git(worktree, "rev-parse", "HEAD").strip()
        try:
            for sha in group.fixups:
                git(worktree, "cherry-pick", "--allow-empty", "--keep-redundant-commits", sha, env=env)
        except RuntimeError as error:
            git(worktree, "cherry-pick", "--abort", check=False)
            git(worktree, "reset", "--hard", "--quiet", before)
            group.status = "fixups_not_applied"
            group.detail = " ".join(str(error).split())[:300]
            record["not_applied"].append(group.name)
            continue
        record["cherry_picked"] += len(group.fixups)
        record["applied"].append(group.name)
    if not record["applied"]:
        return record
    try:
        git(worktree, "rebase", "-i", "--autosquash", "--keep-empty", "--empty=keep", base, env=env)
    except RuntimeError as error:
        git(worktree, "rebase", "--abort", check=False)
        git(worktree, "checkout", "--quiet", "--force", "--detach", tip)
        record["rebase_error"] = " ".join(str(error).split())[:300]
        for group in groups:
            if group.status == "repaired":
                group.status = "fixups_not_applied"
                group.detail = "the autosquash rebase failed: " + record["rebase_error"]
        record["not_applied"] += record["applied"]
        record["applied"] = []
        return record
    for group in groups:
        if group.status == "repaired":
            group.status = "folded"
    record["tip"] = git(worktree, "rev-parse", "HEAD").strip()
    return record


def remaining_findings(findings: dict, groups: list[Group], failed_modules: list[str]) -> dict:
    """What the sequential path still has to repair: the findings of every group not folded, plus a
    ``build`` finding for each module the verification build failed outside those groups."""
    paths = {path for group in groups if group.status != "folded" for path in group.paths}
    remaining = restrict_findings(findings, paths)
    folded = {path for group in groups if group.status == "folded" for path in group.files}
    for module in failed_modules:
        path = _path(module)
        if path in paths:
            continue
        remaining["other"].append(
            {
                "kind": "build",
                "module": path,
                "message": "failed in the worker's whole-library build after the parallel repair"
                + (" of the module's own group" if path in folded else ""),
            }
        )
        if path not in remaining["failed_modules"]:
            remaining["failed_modules"].append(path)
    return remaining


# ----------------------------------------------------------------------------------------------
# Running the groups


def repair_groups(
    cfg,
    task,
    worktree: Path,
    chain: list[ChainCommit],
    groups: list[Group],
    findings: dict,
    context: PromptContext,
    logdir: Path,
    cancel: threading.Event,
    env_lake: dict,
    *,
    providers: list[str],
) -> dict:
    """Run one agent per group concurrently (at most ``cfg.parallel_agents`` at a time, each in a child
    process), fold their fixups into the chain in the task's worktree, validate it and run the
    whole-library build under the lock. ``providers`` is the eligible provider chain the worker
    chose for every group.

    The record's ``status`` is ``ok`` when every group folded, the chain validates and the build
    passed; ``partial`` when the sequential path has work left (``remaining`` carries its findings
    and the task worktree holds the folded chain); ``cancelled``; or a provider status from
    ``PROVIDER_STATUSES`` when no group was repaired and an agent reported it.
    """
    started = time.monotonic()
    tip = task.tip
    base = task.validation_base
    lock = lock_path(worktree)
    record: dict = {"status": "running", "groups": [], "lock": str(lock), "parallel_agents": cfg.parallel_agents}
    landed = repair.landed_on_main(context, chain, cfg.clone)
    workers = max(1, min(int(cfg.parallel_agents), len(groups)))
    group_cancels = {group.name: threading.Event() for group in groups}
    task_packages = worktree / ".lake" / "packages"
    ownership = getattr(context, "repair_context", None)

    def prepare(group: Group) -> Path:
        path = cfg.root / "worktrees" / f"task-{task.task_id}-{group.name}"
        repair.create_worktree(worktree, tip, path)
        if cfg.seed_packages:
            repair.link_shared_packages(path, cfg.seed_packages)
        elif task_packages.is_dir() or task_packages.is_symlink():
            repair.link_shared_packages(path, task_packages.resolve())
        listings = repair.listings_path(path)
        listings.parent.mkdir(parents=True, exist_ok=True)
        listings.write_text(
            "".join(json.dumps(entry) + "\n" for entry in repair.member_records(context, chain, landed))
        )
        if ownership is not None and hasattr(ownership, "members"):
            repair_context.write_files(ownership, path)
        repair_tools.write_build_script(path / repair_tools.TOOLS_DIR, lock=lock)
        group.worktree = str(path)
        return path

    def run(group: Group, path: Path) -> None:
        group_started = time.monotonic()
        group_dir = cfg.tasks_dir / task.task_id / "groups" / group.name
        group_logdir = logdir / group.name
        group_dir.mkdir(parents=True, exist_ok=True)
        group_logdir.mkdir(parents=True, exist_ok=True)
        prompt_path = group_dir / "prompt.md"
        prompt_path.write_text(group_prompt(context, group, chain, cfg.clone, tip))
        result_path = group_dir / "result.json"
        result_path.unlink(missing_ok=True)
        job = {
            "cfg": config_to_json(cfg),
            "task": task.to_json(),
            "worktree": str(path),
            "prompt": str(prompt_path),
            "logdir": str(group_logdir),
            "state_dir": str(group_dir),
            "result": str(result_path),
            "label": group.name,
            "env_lake": env_lake,
            "providers": list(providers),
        }
        job_path = group_dir / "job.json"
        job_path.write_text(json.dumps(job, default=str))
        env = dict(os.environ)
        if cfg.api_key:
            env[API_KEY_ENV] = cfg.api_key
        cancelled = False
        with (group_logdir / "runner.log").open("ab") as log:
            proc = subprocess.Popen(
                [sys.executable, "-m", "tauceti_queue.repair_parallel", "run-group", str(job_path)],
                cwd=REPO_ROOT,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
            )
            while proc.poll() is None:
                if group_cancels[group.name].wait(0.2):
                    cancelled = True
                    proc.terminate()
                    try:
                        proc.wait(60)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait()
                    break
        if result_path.exists():
            group.agent = json.loads(result_path.read_text())
        elif cancelled:
            group.agent = {"status": "cancelled", "attempts": []}
        else:
            group.agent = {"status": "failed", "reason": f"the group runner exited {proc.returncode}", "attempts": []}
        status = group.agent["status"]
        if status == "ok":
            group.fixups, problem = collect_fixups(path, tip, group)
            if problem:
                group.status, group.detail = "unusable", problem
            else:
                group.status = "repaired"
        else:
            group.status = status
            group.detail = group.agent.get("reason")
        group.seconds = round(time.monotonic() - group_started, 1)

    paths = {group.name: prepare(group) for group in groups}
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers, thread_name_prefix="repair-group") as pool:
        futures = [pool.submit(run, group, paths[group.name]) for group in groups]
        while True:
            _done, pending = concurrent.futures.wait(futures, timeout=0.2)
            if cancel.is_set():
                for event in group_cancels.values():
                    event.set()
            if not pending:
                break
    for future in futures:
        future.result()
    record["groups"] = [group.summary() for group in groups]
    record["attempts"] = [a for group in groups for a in (group.agent or {}).get("attempts", [])]
    if cancel.is_set() or any(group.status == "cancelled" for group in groups):
        record.update(status="cancelled", seconds=round(time.monotonic() - started, 1))
        _cleanup(worktree, groups, cfg.keep_worktree)
        return record
    repaired = [group for group in groups if group.status == "repaired"]
    if not repaired:
        provider = next((g.status for g in groups if g.status in PROVIDER_STATUSES), None)
        if provider:
            record.update(status=provider, reason=next(g.detail for g in groups if g.status == provider))
        else:
            record.update(status="partial")
        record["remaining"] = remaining_findings(findings, groups, [])
        record["seconds"] = round(time.monotonic() - started, 1)
        _cleanup(worktree, groups, cfg.keep_worktree)
        return record
    record["fold"] = fold_fixups(worktree, base, tip, groups)
    record["groups"] = [group.summary() for group in groups]
    folded_tip = record["fold"].get("tip")
    failed_modules: list[str] = []
    if folded_tip:
        report = repair.chain_report(cfg.clone, base, tip, folded_tip, task.member_keys, prior_base=task.base)
        record["report"] = report
        if not report["ok"]:
            git(worktree, "checkout", "--quiet", "--force", "--detach", tip)
            for group in groups:
                if group.status == "folded":
                    group.status, group.detail = "fixups_not_applied", "the folded chain did not validate"
            record["groups"] = [group.summary() for group in groups]
        elif not cancel.is_set():
            lock_wait = time.monotonic()
            with repair_tools.build_lock(worktree, log=lambda *a, **k: None):
                record["lock_wait_seconds"] = round(time.monotonic() - lock_wait, 1)
                # CI's offline bwrap sandbox, as every build the worker runs (``repair_sandbox``).
                build = repair.run_logged(
                    repair_sandbox.sandboxed_lake([*cfg.lake, "build", "--iofail"], worktree, env_lake),
                    worktree,
                    logdir / "verify-build.log",
                    env_lake,
                    cfg.build_timeout,
                )
            build.update(repair.parse_lake_build_log(Path(build["log"]).read_text(errors="replace")))
            record["build"] = {k: v for k, v in build.items() if "load" not in k}
            failed_modules = list(build["failed_modules"])
            if build["rc"] != 0 and not failed_modules:
                failed_modules = [path for group in groups if group.status == "folded" for path in group.paths]
    _cleanup(worktree, groups, cfg.keep_worktree)
    if cancel.is_set():
        record["status"] = "cancelled"
    elif all(group.status == "folded" for group in groups) and not failed_modules and record.get("build"):
        record["status"] = "ok"
        record["tip"] = folded_tip
    else:
        record["status"] = "partial"
        record["remaining"] = remaining_findings(findings, groups, failed_modules)
    record["seconds"] = round(time.monotonic() - started, 1)
    return record


def _cleanup(worktree: Path, groups: list[Group], keep: bool) -> None:
    if keep:
        return
    for group in groups:
        if group.worktree:
            with contextlib.suppress(Exception):
                repair.remove_worktree(worktree, Path(group.worktree))


if __name__ == "__main__":
    sys.exit(main())

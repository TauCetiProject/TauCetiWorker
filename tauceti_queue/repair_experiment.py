"""Historical repair experiments for the AI merge queue (increment 0, stream B).

The plan (docs/redesign/specs/ai-merge-queue.md) rests on one untested claim: an agent given a failed
staging candidate with complete diagnostics and warm build artifacts produces a green replacement
chain, and does so faster than the author round-trip. This harness measures that claim on the failed
candidates recorded in the Bors candidate journal, without publishing anything.

Phases, each a separate function so the same harness can later run on live failures:

- selection (``select_candidates``): read journal snapshots, keep failed candidates whose diagnostics
  name compiler, audit or lint findings, group them into failure lineages;
- preparation (``prepare_sources``, ``prepare_artifacts``): fetch the retained ``staging-candidate-N-G``
  commit and the CI job log, make a private detached worktree, restore the published ``main`` cache at
  the candidate's base, and run the incremental build that warms the batch's own modules;
- chain construction (``build_attributed_chain``): the retained commit is already Bors's per-PR squash
  chain on ``base_sha``; the attributed chain re-creates it with ``Source-PR``/``Accepted-Head``
  trailers and identical trees;
- agent execution (``run_agent``): the worker's own ``host_agent_argv`` with the plan's instruction,
  the real diagnostics and the constituent commits;
- measurement (``measure_repair``): validation of the returned chain, per-PR comparison, the confirming
  build, patches, tokens and estimated cost. No network calls.

Only the preparation phase and the agent itself touch the network (GitHub, the Lake cache service and
inference). CLI: ``python -m tauceti_queue.repair_experiment {select,run,report}``.
"""

from __future__ import annotations

import argparse
import collections
import dataclasses
import datetime as dt
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

from tauceti_queue import chain as chain_tool
from tauceti_queue.repair import (  # noqa: F401  (re-exported for the harness's callers and tests)
    AGENT_INSTRUCTION,
    ANSI,
    CACHE_DOMAIN,
    CACHE_SERVICE,
    DEFAULT_REPOSITORY,
    FAILED_MODULE,
    MODULE_PATH,
    STAMP,
    SUBJECT_PR,
    ChainCommit,
    PromptContext,
    agent_environment,
    agent_usage,
    checkout_worktree,
    compare_chains,
    create_worktree,
    declaration_module,
    findings_kinds,
    findings_modules,
    git,
    host_state,
    lake_env,
    link_shared_packages,
    listings_path,
    module_to_path,
    owners_of,
    parse_lake_build_log,
    prune_pool,
    read_chain,
    remove_worktree,
    run_agent,
    run_logged,
    split_trailers,
    strip_log,
    validate_chain,
    write_record,
)
from tauceti_queue.repair import compose_prompt as _compose_prompt
from tauceti_queue.repair import prepare_artifacts as _prepare_artifacts

REPO = DEFAULT_REPOSITORY
JOURNAL_URL = "https://bors.taucetiproject.org/repositories/1/candidates?base=main"
REF_PREFIX = "refs/repair-experiments"
DEFAULT_ROOT = Path(os.environ.get("TAUCETI_REPAIR_ROOT") or Path.home() / "tmp" / "repair-experiments")
DEFAULT_CLONE = Path(os.environ.get("TAUCETI_REPAIR_CLONE") or Path.home() / "TauCeti")


def _split_trailers(clone: Path, message: str) -> tuple[str, dict[str, str]]:
    """The harness's original signature; the clone is not needed."""
    return split_trailers(message)


_kinds_from_findings = findings_kinds

# Journal observation kinds that make a failed candidate eligible, and the failure kind they imply.
OBSERVATION_KINDS = {
    "compiler_error": "compiler",
    "duplicate_declaration": "audit",
    "environment_linter": "lint",
}

# ----------------------------------------------------------------------------------------------
# Journal selection


@dataclasses.dataclass
class Member:
    pr: int
    head_sha: str
    admitted_at: int | None
    parent_candidate_id: int | None
    retry_reason: str | None


@dataclasses.dataclass
class Observation:
    kind: str
    paths: list[str]
    declaration: str | None = None
    linter: str | None = None
    unknown: str | None = None

    def modules(self) -> list[str]:
        """Module paths this observation names: explicit paths, or the defining module of a declaration."""
        if self.paths:
            return list(self.paths)
        if self.declaration:
            module = declaration_module(self.declaration)
            if module:
                return [module]
        return []


@dataclasses.dataclass
class Failure:
    candidate_id: int
    generation: int
    batch_id: int
    base_sha: str
    commit: str | None
    toolchain: str | None
    members: list[Member]
    observations: list[Observation]
    suspects: list[int]
    suspect_heads: dict[int, str]
    strategy: str | None
    run_id: int | None
    job_ids: list[int]
    runner_started_at: int | None
    runner_finished_at: int | None
    lineage: int | None = None

    @property
    def kinds(self) -> list[str]:
        kinds = {OBSERVATION_KINDS[o.kind] for o in self.observations if o.kind in OBSERVATION_KINDS}
        return sorted(kinds, key=["compiler", "audit", "lint"].index)

    @property
    def failing_modules(self) -> list[str]:
        seen: dict[str, None] = {}
        for observation in self.observations:
            for module in observation.modules():
                seen.setdefault(module, None)
        return list(seen)

    @property
    def member_prs(self) -> list[int]:
        return [m.pr for m in self.members]

    @property
    def ref(self) -> str:
        return f"staging-candidate-{self.candidate_id}-{self.generation}"

    def to_json(self) -> dict:
        record = dataclasses.asdict(self)
        record["kinds"] = self.kinds
        record["failing_modules"] = self.failing_modules
        return record


def load_journal(paths: list[Path]) -> dict[int, dict]:
    """Merge candidate snapshots by id; a later file wins for the same candidate (it has more events)."""
    rows: dict[int, dict] = {}
    for path in paths:
        payload = json.loads(Path(path).read_text())
        candidates = payload["candidates"] if isinstance(payload, dict) else payload
        for candidate in candidates:
            rows[int(candidate["id"])] = candidate
    return rows


def _events(candidate: dict, kind: str) -> list[dict]:
    return [e for e in candidate.get("events", []) if e.get("kind") == kind]


def failure_evidence(candidate: dict) -> Failure | None:
    """The candidate's failure as the journal records it, or None when it is not an eligible failure.

    Eligible means: the trusted failure classification is ``candidate_failure`` (not a construction
    conflict, infrastructure failure or cancellation), and the terminal-log diagnostics record at least
    one compiler, duplicate-declaration or environment-lint observation.
    """
    if any(e.get("source") == "github.base_push_response" and e.get("kind") == "promoted" for e in candidate["events"]):
        return None
    classifications = {
        e["details"].get("classification")
        for e in _events(candidate, "failure_classified")
        if e.get("source") == "bors.trusted_failure"
    }
    if "candidate_failure" not in classifications:
        return None
    observations: list[Observation] = []
    run_id = None
    job_ids: list[int] = []
    for event in _events(candidate, "failure_diagnostics"):
        details = event.get("details") or {}
        binding = details.get("binding") or {}
        run_id = run_id or binding.get("run_id")
        for job in binding.get("job_ids") or []:
            if job not in job_ids:
                job_ids.append(job)
        result = details.get("result") or {}
        for job in result.get("jobs") or []:
            for raw in job.get("observations") or []:
                observation = Observation(
                    kind=str(raw.get("kind")),
                    paths=list(raw.get("paths") or []),
                    declaration=raw.get("declaration"),
                    linter=raw.get("linter"),
                    unknown=raw.get("unknown"),
                )
                if observation not in observations:
                    observations.append(observation)
    if not any(o.kind in OBSERVATION_KINDS for o in observations):
        return None
    suspects: list[int] = []
    suspect_heads: dict[int, str] = {}
    strategy = None
    for event in _events(candidate, "failure_partition"):
        details = event.get("details") or {}
        strategy = details.get("strategy") or strategy
        groups = details.get("groups") or []
        if groups and len(groups) > 1 and details.get("ownership_complete"):
            for pr in groups[0]:
                if pr not in suspects:
                    suspects.append(int(pr))
    for event in _events(candidate, "provisional_repair"):
        details = event.get("details") or {}
        prs = details.get("prs") or ([details["pr"]] if details.get("pr") is not None else [])
        for pr in prs:
            pr = int(pr)
            if pr not in suspects:
                suspects.append(pr)
        if details.get("pr") is not None and details.get("head_sha"):
            suspect_heads[int(details["pr"])] = details["head_sha"]
    constructed = _events(candidate, "constructed")
    commit = toolchain = None
    if constructed:
        details = constructed[-1].get("details") or {}
        commit = details.get("commit")
        toolchain = ((details.get("toolchain") or {}).get("content") or "").strip() or None
    snapshot = candidate["snapshot"]
    members = [
        Member(
            pr=int(m["pr"]),
            head_sha=m["head_sha"],
            admitted_at=m.get("original_admitted_at"),
            parent_candidate_id=m.get("parent_candidate_id"),
            retry_reason=m.get("retry_reason"),
        )
        for m in snapshot["members"]
    ]
    for member in members:
        if member.pr in suspects:
            suspect_heads.setdefault(member.pr, member.head_sha)
    return Failure(
        candidate_id=int(candidate["id"]),
        generation=int(candidate["generation"]),
        batch_id=int(candidate["batch_id"]),
        base_sha=snapshot["base_sha"],
        commit=commit,
        toolchain=toolchain,
        members=members,
        observations=observations,
        suspects=suspects,
        suspect_heads=suspect_heads,
        strategy=strategy,
        run_id=run_id,
        job_ids=job_ids,
        runner_started_at=candidate.get("runner_started_at"),
        runner_finished_at=candidate.get("runner_finished_at"),
    )


def lineage_root(candidate_id: int, journal: dict[int, dict]) -> int:
    """Follow the members' recorded parent candidate up through the journal.

    A candidate's lineage root is its oldest known ancestor: the parent chain is followed while the
    parent is in the journal, and the first parent outside the journal (or the candidate itself when
    its members have no parent) is the root.
    """
    seen = {candidate_id}
    current = candidate_id
    while True:
        candidate = journal.get(current)
        if candidate is None:
            return current
        parents = collections.Counter(
            m.get("parent_candidate_id") for m in candidate["snapshot"]["members"] if m.get("parent_candidate_id")
        )
        if not parents:
            return current
        parent = parents.most_common(1)[0][0]
        if parent in seen:
            return current
        seen.add(parent)
        current = parent


def select_candidates(journal: dict[int, dict], include: tuple[int, ...] = ()) -> list[Failure]:
    """Eligible failures with lineage roots, ordered by candidate id.

    ``include`` adds candidates whose journal diagnostics are incomplete (for example the duplicate
    declaration audit the parser missed, staging-triage.md change 4) when the raw CI log still has the
    findings; they are marked with an empty observation list and their diagnostics come from the log.
    """
    selected: list[Failure] = []
    for candidate_id in sorted(journal):
        candidate = journal[candidate_id]
        failure = failure_evidence(candidate)
        if failure is None and candidate_id in include:
            failure = _forced_failure(candidate)
        if failure is None:
            continue
        failure.lineage = lineage_root(candidate_id, journal)
        selected.append(failure)
    return selected


def _forced_failure(candidate: dict) -> Failure | None:
    constructed = _events(candidate, "constructed")
    classified = _events(candidate, "failure_classified")
    if not constructed or not any(e["details"].get("classification") == "candidate_failure" for e in classified):
        return None
    details = constructed[-1]["details"]
    snapshot = candidate["snapshot"]
    run_id = None
    job_ids: list[int] = []
    for event in _events(candidate, "failure_diagnostics"):
        binding = (event.get("details") or {}).get("binding") or {}
        run_id = run_id or binding.get("run_id")
        job_ids.extend(j for j in binding.get("job_ids") or [] if j not in job_ids)
    return Failure(
        candidate_id=int(candidate["id"]),
        generation=int(candidate["generation"]),
        batch_id=int(candidate["batch_id"]),
        base_sha=snapshot["base_sha"],
        commit=details.get("commit"),
        toolchain=((details.get("toolchain") or {}).get("content") or "").strip() or None,
        members=[
            Member(
                int(m["pr"]),
                m["head_sha"],
                m.get("original_admitted_at"),
                m.get("parent_candidate_id"),
                m.get("retry_reason"),
            )
            for m in snapshot["members"]
        ],
        observations=[],
        suspects=[],
        suspect_heads={},
        strategy=None,
        run_id=run_id,
        job_ids=job_ids,
        runner_started_at=candidate.get("runner_started_at"),
        runner_finished_at=candidate.get("runner_finished_at"),
    )


def lineages(failures: list[Failure]) -> dict[int, list[int]]:
    groups: dict[int, list[int]] = collections.defaultdict(list)
    for failure in failures:
        groups[failure.lineage if failure.lineage is not None else failure.candidate_id].append(failure.candidate_id)
    return dict(sorted(groups.items()))


def selection_table(failures: list[Failure]) -> str:
    lines = [
        "| candidate | gen | lineage | members | kind | failing modules | suspects |",
        "| ---: | ---: | ---: | ---: | --- | --- | --- |",
    ]
    for f in failures:
        modules = ", ".join(f.failing_modules) or "(from CI log)"
        lines.append(
            f"| {f.candidate_id} | {f.generation} | {f.lineage} | {len(f.members)} | {'+'.join(f.kinds) or '?'} | {modules} | "
            f"{', '.join(str(p) for p in f.suspects) or '-'} |"
        )
    return "\n".join(lines)


# ----------------------------------------------------------------------------------------------
# CI log findings (pure text processing)

COMPILER_ERROR = re.compile(r"^(?:error: )?(" + MODULE_PATH.pattern + r"):(\d+):(\d+):\s*(?:error:\s*)?(.*)$")
DUPLICATE = re.compile(r"^duplicate-declarations: (\S+) is declared in both (\S+) and (\S+)")
COLLISION = re.compile(r"^audit-driver: import (\S+) failed, environment already contains '([^']+)' from (\S+)")
LINT_LOCATIONS = re.compile(r"^LINT-ENV-LOCATIONS (\{.*\})\s*$")


def extract_log_findings(text: str) -> dict:
    """Pull the agent-facing diagnostics out of a sandboxed-build job log.

    Compiler errors keep their message and the indented continuation lines Lean prints after them;
    audit and lint findings keep the driver's own line. Counters describe the build the log records.
    """
    lines = strip_log(text)
    compiler_errors: list[dict] = []
    duplicates: list[dict] = []
    collisions: list[dict] = []
    lint: dict = {"findings": [], "report": []}
    failed_modules: list[str] = []
    built = canceled = 0
    index = 0
    while index < len(lines):
        line = lines[index]
        if "Built TauCeti" in line:
            built += 1
        elif "] Canceled " in line:
            canceled += 1
        failed = FAILED_MODULE.match(line)
        if failed and failed.group(1) not in failed_modules:
            failed_modules.append(failed.group(1))
        error = COMPILER_ERROR.match(line)
        if error and ("error" in line.split(":", 1)[0] or line.startswith(error.group(1))):
            message = [error.group(4)]
            while index + 1 < len(lines) and lines[index + 1].startswith((" ", "\t")):
                index += 1
                message.append(lines[index])
            entry = {
                "path": error.group(1),
                "line": int(error.group(2)),
                "column": int(error.group(3)),
                "message": "\n".join(message).strip(),
            }
            if entry not in compiler_errors:
                compiler_errors.append(entry)
        duplicate = DUPLICATE.match(line)
        if duplicate:
            entry = {"declaration": duplicate.group(1), "modules": [duplicate.group(2), duplicate.group(3)]}
            if entry not in duplicates:
                duplicates.append(entry)
        collision = COLLISION.match(line)
        if collision:
            entry = {
                "module": collision.group(1),
                "declaration": collision.group(2),
                "other_module": collision.group(3),
            }
            if entry not in collisions:
                collisions.append(entry)
        locations = LINT_LOCATIONS.match(line)
        if locations:
            try:
                lint["findings"] = json.loads(locations.group(1)).get("findings", [])
            except json.JSONDecodeError:
                pass
        if line.startswith("lint-env: NEW violation"):
            block = [line]
            while index + 1 < len(lines) and not lines[index + 1].startswith(("##[", "LINT-ENV:")):
                index += 1
                block.append(lines[index])
            lint["report"] = block
        index += 1
    return {
        "compiler_errors": compiler_errors,
        "duplicate_declarations": duplicates,
        "import_collisions": collisions,
        "lint": lint,
        "failed_modules": failed_modules,
        "built_modules": built,
        "canceled_jobs": canceled,
    }


def effective_kinds(failure: Failure, findings: dict | None) -> list[str]:
    """Failure kinds from the journal, or from the CI log when the journal recorded no observation."""
    kinds = set(failure.kinds)
    if not kinds and findings:
        if findings.get("compiler_errors"):
            kinds.add("compiler")
        if findings.get("duplicate_declarations") or findings.get("import_collisions"):
            kinds.add("audit")
        if (findings.get("lint") or {}).get("findings"):
            kinds.add("lint")
    return sorted(kinds, key=["compiler", "audit", "lint"].index)


# ----------------------------------------------------------------------------------------------
# Git helpers


def ensure_candidate_ref(clone: Path, failure: Failure) -> str:
    """Fetch the retained candidate branch into a private ref and return its sha (network)."""
    ref = f"{REF_PREFIX}/{failure.ref}"
    sha = git(clone, "rev-parse", "--verify", "--quiet", ref, check=False).strip()
    if not sha:
        git(clone, "fetch", "--quiet", "origin", f"+refs/heads/{failure.ref}:{ref}")
        sha = git(clone, "rev-parse", "--verify", ref).strip()
    if failure.commit and sha != failure.commit:
        raise RuntimeError(f"retained ref {failure.ref} is {sha}, journal recorded {failure.commit}")
    if git(clone, "cat-file", "-t", failure.base_sha, check=False).strip() != "commit":
        git(clone, "fetch", "--quiet", "origin", failure.base_sha)
    return sha


def build_attributed_chain(
    clone: Path, base: str, retained_tip: str, members: list[Member]
) -> tuple[str, list[ChainCommit]]:
    """Re-create Bors's squash chain with ``Source-PR``/``Accepted-Head`` trailers and identical trees.

    This is the minimal stand-in for stream A's ``tauceti_queue.chain`` builder: the retained commit is
    already one squash commit per member on ``base_sha``, so attribution is a reword that preserves
    trees, authorship and order, and the chain tip's tree equals the retained tree by construction.
    """
    retained = read_chain(clone, base, retained_tip)
    heads = {m.pr: m.head_sha for m in members}
    chain_prs = [c.pr for c in retained]
    missing = sorted(set(heads) - set(chain_prs))
    extra = sorted(set(chain_prs) - set(heads))
    if missing or extra or len(chain_prs) != len(set(chain_prs)):
        raise RuntimeError(f"chain/member mismatch: missing {missing}, unexpected {extra}, commits {len(chain_prs)}")
    parent = git(clone, "rev-parse", f"{base}^{{commit}}").strip()
    for commit in retained:
        message = git(
            clone,
            "interpret-trailers",
            "--if-exists",
            "replace",
            "--trailer",
            f"Source-PR: {REPO}#{commit.pr}",
            "--trailer",
            f"Accepted-Head: {heads[commit.pr]}",
            input=commit.message,
        )
        author = git(clone, "show", "-s", "--format=%an%x00%ae%x00%aD%x00%cn%x00%ce%x00%cD", commit.sha).split("\x00")
        env = {
            **os.environ,
            "GIT_AUTHOR_NAME": author[0],
            "GIT_AUTHOR_EMAIL": author[1],
            "GIT_AUTHOR_DATE": author[2],
            "GIT_COMMITTER_NAME": author[3],
            "GIT_COMMITTER_EMAIL": author[4],
            "GIT_COMMITTER_DATE": author[5].strip(),
        }
        parent = git(clone, "commit-tree", commit.tree, "-p", parent, "-m", message, env=env).strip()
    tip = parent
    chain = read_chain(clone, base, tip)
    problems = validate_chain(clone, base, tip, [m.pr for m in members], chain)
    if problems:
        raise RuntimeError("attributed chain failed validation: " + "; ".join(problems))
    return tip, chain


# ----------------------------------------------------------------------------------------------
# Host state and worktrees


# ----------------------------------------------------------------------------------------------
# Preparation (network: GitHub, the Lake cache service)


def fetch_job_log(job_id: int, dest: Path) -> Path | None:
    if dest.exists() and dest.stat().st_size > 0:
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        ["gh", "api", "--allow-escape-sequences", f"repos/{REPO}/actions/jobs/{job_id}/logs"],
        capture_output=True,
        check=False,
    )
    if result.returncode != 0 or not result.stdout:
        return None
    dest.write_bytes(result.stdout)
    return dest


def fetch_member_heads(clone: Path, members: list[Member], batch: int = 40) -> int:
    """Fetch the accepted heads that are not yet in the clone (network); returns how many were fetched."""
    missing = [m.head_sha for m in members if git(clone, "cat-file", "-t", m.head_sha, check=False).strip() != "commit"]
    for index in range(0, len(missing), batch):
        git(clone, "fetch", "--quiet", "origin", *missing[index : index + batch])
    return len(missing)


def construct_chain(clone: Path, failure: Failure, retained_sha: str) -> tuple[str, list[ChainCommit], dict]:
    """The attributed chain the agent edits, built with the chain tool from the accepted heads.

    Bors's retained commit is already one squash commit per member on ``base_sha``; its subjects and
    bodies (the PR titles and descriptions) become the chain tool's member titles and descriptions.
    When the tool's chain is clean and its tip tree equals the retained tree it is used as is. When
    it conflicts or its tree differs, the difference is recorded and the retained chain is reworded
    with the trailers instead, so the agent always starts from the exact tree CI built and failed.
    """
    retained = read_chain(clone, failure.base_sha, retained_sha)
    by_pr = {c.pr: c for c in retained}
    heads = {m.pr: m.head_sha for m in failure.members}
    missing = sorted(set(heads) - set(by_pr))
    extra = sorted(set(by_pr) - set(heads))
    if missing or extra:
        raise RuntimeError(f"retained chain/member mismatch: missing {missing}, unexpected {extra}")
    members = []
    for commit in retained:
        subject = SUBJECT_PR.sub("", commit.subject).strip()
        body = commit.message.split("\n", 1)[1].strip() if "\n" in commit.message else ""
        members.append(
            chain_tool.Member(REPO, commit.pr, heads[commit.pr], title=subject, description=body, coauthors=())
        )
    record: dict = {"tool": "tauceti_queue.chain.build"}
    retained_tree = git(clone, "rev-parse", f"{retained_sha}^{{tree}}").strip()
    try:
        built = chain_tool.build(clone, failure.base_sha, members, chain_id=f"repair-experiment-{failure.ref}")
        record["status"] = built.status
        record["tip"] = built.tip
        record["ref"] = built.ref
        conflict = built.conflict
        if conflict is not None:
            record["conflict"] = conflict.to_dict()
        built_tree = git(clone, "rev-parse", f"{built.tip}^{{tree}}").strip()
        record["tree_matches_retained"] = built.status == chain_tool.CLEAN and built_tree == retained_tree
        if built.status == chain_tool.CLEAN and not record["tree_matches_retained"]:
            record["tree_difference"] = git(clone, "diff", "--stat", "--no-color", built.tip, retained_sha).strip()[
                -2000:
            ]
            divergent = []
            for record_commit, retained_commit in zip(built.commits, retained, strict=False):
                if (
                    record_commit.sha
                    and git(clone, "rev-parse", f"{record_commit.sha}^{{tree}}").strip() != retained_commit.tree
                ):
                    divergent.append(retained_commit.pr)
            record["first_divergent_prs"] = divergent[:5]
    except chain_tool.ChainError as error:
        record["status"] = "error"
        record["error"] = str(error)
        record["tree_matches_retained"] = False
    if record.get("tree_matches_retained"):
        tip = record["tip"]
        source = "chain.build"
    else:
        tip, _ = build_attributed_chain(clone, failure.base_sha, retained_sha, failure.members)
        source = "retained_reword"
    chain = read_chain(clone, failure.base_sha, tip)
    report = chain_tool.validate(clone, failure.base_sha, tip, [m.source_pr for m in members])
    if not report.ok:
        raise RuntimeError("chain validation failed: " + "; ".join(v.detail for v in report.violations))
    record["source"] = source
    record["validate_ok"] = report.ok
    return tip, chain, record


def prepare_sources(clone: Path, failure: Failure, experiment_dir: Path, worktree: Path, seed_packages: Path) -> dict:
    """Retained commit, member heads, CI log, attributed chain and the detached worktree at its tip."""
    record: dict = {}
    started = time.monotonic()
    sha = ensure_candidate_ref(clone, failure)
    record["retained_sha"] = sha
    record["retained_tree"] = git(clone, "rev-parse", f"{sha}^{{tree}}").strip()
    record["member_heads_fetched"] = fetch_member_heads(clone, failure.members)
    logs = []
    for job in failure.job_ids:
        path = fetch_job_log(job, experiment_dir / "ci" / f"job-{job}.log")
        if path:
            logs.append(str(path))
    record["ci_logs"] = logs
    findings = {
        "compiler_errors": [],
        "duplicate_declarations": [],
        "import_collisions": [],
        "lint": {"findings": [], "report": []},
        "failed_modules": [],
        "built_modules": 0,
        "canceled_jobs": 0,
    }
    for path in logs:
        found = extract_log_findings(Path(path).read_text(errors="replace"))
        for key in ("compiler_errors", "duplicate_declarations", "import_collisions", "failed_modules"):
            for item in found[key]:
                if item not in findings[key]:
                    findings[key].append(item)
        if found["lint"]["findings"] or found["lint"]["report"]:
            findings["lint"] = found["lint"]
        findings["built_modules"] = max(findings["built_modules"], found["built_modules"])
        findings["canceled_jobs"] = max(findings["canceled_jobs"], found["canceled_jobs"])
    record["ci_findings"] = findings
    (experiment_dir / "ci-findings.json").write_text(json.dumps(findings, indent=1))
    tip, chain, chain_record = construct_chain(clone, failure, sha)
    chain_ref = f"{REF_PREFIX}/chain/{failure.ref}"
    git(clone, "update-ref", chain_ref, tip)
    record["chain_tool"] = chain_record
    record["chain_tip"] = tip
    record["chain_ref"] = chain_ref
    record["chain_source"] = chain_record["source"]
    record["chain_tree_matches_retained"] = (
        git(clone, "rev-parse", f"{tip}^{{tree}}").strip() == record["retained_tree"]
    )
    record["chain_length"] = len(chain)
    record["worktree_reused"] = checkout_worktree(clone, tip, worktree)
    link_shared_packages(worktree, seed_packages)
    record["seconds"] = round(time.monotonic() - started, 1)
    return record


def prepare_artifacts(worktree: Path, failure: Failure, env: dict, logdir: Path, build_timeout: float) -> dict:
    """Warm artifacts the way the plan's default source does (``tauceti_queue.repair.prepare_artifacts``),
    probing as well whether the failed candidate's own outputs were published."""
    return _prepare_artifacts(
        worktree, failure.base_sha, env, logdir, build_timeout, repository=REPO, candidate=failure.commit or "HEAD"
    )


# ----------------------------------------------------------------------------------------------
# Agent


def compose_prompt(
    failure: Failure,
    findings: dict,
    chain: list[ChainCommit],
    clone: Path,
    base_short: str,
    *,
    worktree: Path | None = None,
    artifacts: dict | None = None,
) -> str:
    """The plan's instruction, then the candidate's real diagnostics and its constituent commits.
    With a ``worktree`` a large batch's full listings go to ``repair.listings_path``; ``artifacts``
    (the ``prepare_artifacts`` record) says whether the build is cold."""
    cold = bool(artifacts) and not artifacts.get("available", True)
    context = PromptContext(
        repository=REPO,
        base=failure.base_sha,
        origin=f"This batch was staging candidate {failure.candidate_id} and failed CI.",
        suspects=list(failure.suspects),
        suspect_heads=dict(failure.suspect_heads),
        named_modules=failure.failing_modules,
        artifacts_cold=cold,
        artifacts_reason=(artifacts or {}).get("unavailable_reason") if cold else None,
        listings_path=listings_path(worktree) if worktree is not None else None,
    )
    return _compose_prompt(context, findings, chain, clone)


# ----------------------------------------------------------------------------------------------
# Measurement (no network)


def measure_repair(
    clone: Path,
    worktree: Path,
    failure: Failure,
    chain_before: list[ChainCommit],
    env: dict,
    experiment_dir: Path,
    build_timeout: float,
    run_lint: bool = True,
) -> dict:
    """Validate the returned chain, compare per PR, run the confirming build, store patches."""
    record: dict = {}
    dirty = git(worktree, "status", "--porcelain", "--untracked-files=no").strip()
    record["worktree_dirty"] = bool(dirty)
    tip = git(worktree, "rev-parse", "HEAD").strip()
    record["result_tip"] = tip
    result_ref = f"{REF_PREFIX}/result/{failure.ref}"
    git(clone, "update-ref", result_ref, tip)
    record["result_ref"] = result_ref
    record["tip_changed"] = tip != chain_before[-1].sha if chain_before else None
    problems = validate_chain(clone, failure.base_sha, tip, failure.member_prs)
    tool_members = [f"{REPO}#{pr}" for pr in failure.member_prs]
    try:
        tool_report = chain_tool.validate(clone, failure.base_sha, tip, tool_members)
        record["chain_tool_validate"] = tool_report.to_dict()
        problems += [f"chain tool: {v.detail}" for v in tool_report.violations if v.detail not in " ".join(problems)]
    except chain_tool.ChainError as error:
        problems.append(f"chain tool validate: {error}")
    record["validation_problems"] = problems
    comparison: list[dict] = []
    try:
        chain_after = read_chain(clone, failure.base_sha, tip)
        comparison = compare_chains(clone, chain_before, chain_after)
    except RuntimeError as error:
        record["validation_problems"] = problems + [f"chain unreadable: {error}"]
    tool_edited: list[int] | None = None
    if chain_before:
        try:
            tool_compare = chain_tool.compare(clone, failure.base_sha, chain_before[-1].sha, failure.base_sha, tip)
            record["chain_tool_compare"] = tool_compare.to_dict()
            tool_edited = sorted(
                int(m.source_pr.rsplit("#", 1)[1]) for m in tool_compare.members if m.provenance == chain_tool.EDITED
            )
            replay_edited = {int(m.source_pr.rsplit("#", 1)[1]) for m in tool_compare.members if m.content_changed}
            for row in comparison:
                row["content_changed_vs_replay"] = row["pr"] in replay_edited
        except chain_tool.ChainError as error:
            record["chain_tool_compare_error"] = str(error)
    record["comparison"] = comparison
    edited = [row["pr"] for row in comparison if row.get("status") == "edited"]
    record["edited_prs_patch_id"] = edited
    record["edited_prs"] = tool_edited if tool_edited is not None else edited
    record["shrink_prs"] = [row["pr"] for row in comparison if row.get("shrink")]
    record["emptied_prs"] = [row["pr"] for row in comparison if row.get("empty_after") and not row.get("empty_before")]
    record["messages_preserved"] = all(row.get("message_preserved", True) for row in comparison)
    record["edited_without_explanation"] = [
        row["pr"] for row in comparison if row.get("status") == "edited" and not row.get("explanation")
    ]
    patches = experiment_dir / "patches"
    patches.mkdir(parents=True, exist_ok=True)
    if chain_before:
        full = git(clone, "diff", "--no-color", chain_before[-1].sha, tip)
        (patches / "repair.patch").write_text(full)
        record["diffstat"] = git(clone, "diff", "--shortstat", "--no-color", chain_before[-1].sha, tip).strip()
        record["changed_files"] = git(clone, "diff", "--name-only", chain_before[-1].sha, tip).split()
        for row in comparison:
            if row.get("status") == "edited":
                (patches / f"pr-{row['pr']}-before.patch").write_text(
                    git(clone, "show", "--no-color", "--format=%B", row["before"])
                )
                (patches / f"pr-{row['pr']}-after.patch").write_text(
                    git(clone, "show", "--no-color", "--format=%B", row["after"])
                )
    logdir = experiment_dir / "logs"
    build = run_logged(
        ["lake", "build", "--iofail"], worktree, logdir / "confirm-build.log", env=env, timeout=build_timeout
    )
    build.update(parse_lake_build_log(Path(build["log"]).read_text(errors="replace")))
    record["confirm_build"] = build
    record["green"] = build["rc"] == 0
    findings_path = experiment_dir / "ci-findings.json"
    kinds = effective_kinds(failure, json.loads(findings_path.read_text()) if findings_path.exists() else None)
    record["kinds"] = kinds
    if build["rc"] == 0 and run_lint and "lint" in kinds:
        # CI lints the modules the batch changed (staging-triage.md: "linted 165 changed module(s)");
        # an unscoped run also reports violations already on main outside the batch.
        changed = [
            path[: -len(".lean")].replace("/", ".")
            for path in git(clone, "diff", "--name-only", failure.base_sha, tip).split()
            if path.startswith("TauCeti/") and path.endswith(".lean") and (worktree / path).exists()
        ]
        modules_file = experiment_dir / "lint-modules.txt"
        modules_file.write_text("".join(f"{m}\n" for m in changed))
        lint = run_logged(
            ["bash", "scripts/lint-env.sh"],
            worktree,
            logdir / "confirm-lint-env.log",
            env={**env, "LINT_ONLY_MODULES": str(modules_file)},
            timeout=build_timeout,
        )
        lint["scoped_modules"] = len(changed)
        record["confirm_lint"] = lint
        record["green"] = lint["rc"] == 0
    if build["rc"] == 0 and "audit" in kinds:
        record["duplicate_check"] = duplicate_declaration_check(worktree, experiment_dir)
        record["green"] = record["green"] and record["duplicate_check"]["ok"]
    record["host"] = host_state()
    return record


def duplicate_declaration_check(worktree: Path, experiment_dir: Path) -> dict:
    """Textual stand-in for the duplicate-declaration audit: each reported name must be declared in at
    most one of the two modules (counting `theorem|lemma|def|instance|abbrev|structure` heads)."""
    findings_path = experiment_dir / "ci-findings.json"
    findings = json.loads(findings_path.read_text()) if findings_path.exists() else {}
    rows = []
    ok = True
    for duplicate in findings.get("duplicate_declarations", []):
        short = duplicate["declaration"].rsplit(".", 1)[-1]
        pattern = re.compile(
            r"^\s*(?:@\[[^\]]*\]\s*)?(?:(?:private|protected|noncomputable|scoped|nonrec)\s+)*(?:theorem|lemma|def|instance|abbrev|structure|class|inductive)\s+(?:[\w'.]*\.)?"
            + re.escape(short)
            + r"\b"
        )
        counts = {}
        for module in duplicate["modules"]:
            path = worktree / module_to_path(module)
            counts[module] = (
                sum(1 for line in path.read_text(errors="replace").splitlines() if pattern.match(line))
                if path.exists()
                else 0
            )
        declared_in = [m for m, n in counts.items() if n]
        rows.append({"declaration": duplicate["declaration"], "declared_in": declared_in, "counts": counts})
        ok = ok and len(declared_in) <= 1
    for collision in findings.get("import_collisions", []):
        short = collision["declaration"].rsplit(".", 1)[-1]
        pattern = re.compile(
            r"^\s*(?:@\[[^\]]*\]\s*)?(?:(?:private|protected|noncomputable|scoped|nonrec)\s+)*(?:theorem|lemma|def|instance|abbrev|structure|class|inductive)\s+(?:[\w'.]*\.)?"
            + re.escape(short)
            + r"\b"
        )
        counts = {}
        for module in (collision["module"], collision["other_module"]):
            path = worktree / module_to_path(module)
            counts[module] = (
                sum(1 for line in path.read_text(errors="replace").splitlines() if pattern.match(line))
                if path.exists()
                else 0
            )
        declared_in = [m for m, n in counts.items() if n]
        rows.append({"declaration": collision["declaration"], "declared_in": declared_in, "counts": counts})
        ok = ok and len(declared_in) <= 1
    return {"ok": ok, "rows": rows, "note": "textual check; the audit driver itself is not re-run locally"}


# ----------------------------------------------------------------------------------------------
# Orchestration


def run_experiment(
    failure: Failure,
    *,
    root: Path = DEFAULT_ROOT,
    clone: Path = DEFAULT_CLONE,
    seed_packages: Path | None = None,
    agent_timeout: float = 3 * 3600,
    build_timeout: float = 3 * 3600,
    skip_agent: bool = False,
    keep_worktree: bool = True,
    min_free_gb: float = 25.0,
) -> dict:
    """One historical repair experiment, end to end, with a progressively written result record.

    The worktree is per base commit and kept by default, so the next candidate on the same base
    reuses the 14 GB restore of main's cache; ``clean`` removes the worktrees at the end.
    """
    experiment_dir = root / str(failure.candidate_id)
    experiment_dir.mkdir(parents=True, exist_ok=True)
    record_path = experiment_dir / "result.json"
    worktree = root / "worktrees" / f"base-{failure.base_sha[:12]}"
    seed_packages = seed_packages or (root / "seed" / "packages")
    record: dict = {
        "candidate": failure.to_json(),
        "status": "starting",
        "started_at": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
        "host_start": host_state(),
    }
    write_record(record_path, record)
    if record["host_start"]["disk_free_gb"] < min_free_gb:
        record["status"] = "aborted_low_disk"
        write_record(record_path, record)
        return record
    env = lake_env(root)
    try:
        record["sources"] = prepare_sources(clone, failure, experiment_dir, worktree, seed_packages)
        record["status"] = "sources_ready"
        write_record(record_path, record)
        chain_before = read_chain(clone, failure.base_sha, record["sources"]["chain_tip"])
        record["artifacts"] = prepare_artifacts(worktree, failure, env, experiment_dir / "logs", build_timeout)
        record["status"] = "artifacts_ready"
        record["host_after_preparation"] = host_state()
        write_record(record_path, record)
        if record["host_after_preparation"]["disk_free_gb"] < min_free_gb:
            # The host is shared with the worker fleet: no agent starts unless the preparation left
            # the agreed headroom. The worktree is removed so the next attempt starts clean.
            record["status"] = "aborted_low_disk_after_preparation"
            keep_worktree = False
            write_record(record_path, record)
            return record
        findings = record["sources"]["ci_findings"]
        prompt = compose_prompt(
            failure,
            findings,
            chain_before,
            clone,
            failure.base_sha[:12],
            worktree=worktree,
            artifacts=record["artifacts"],
        )
        (experiment_dir / "prompt.md").write_text(prompt)
        record["prompt_chars"] = len(prompt)
        if skip_agent:
            record["status"] = "prepared_only"
            write_record(record_path, record)
            return record
        argv, agent_env, stdin_text = agent_environment(prompt, root, env, agent_timeout)
        record["agent_argv"] = [a if a != prompt else "<prompt>" for a in argv]
        record["status"] = "agent_running"
        record["agent_started_at"] = dt.datetime.now(dt.UTC).isoformat(timespec="seconds")
        write_record(record_path, record)
        record["agent"] = run_agent(worktree, argv, agent_env, experiment_dir / "logs", agent_timeout, stdin_text)
        record["status"] = "measuring"
        write_record(record_path, record)
        record["result"] = measure_repair(clone, worktree, failure, chain_before, env, experiment_dir, build_timeout)
        record["status"] = "complete"
    except Exception as error:  # the record must survive any failure for the report
        record["status"] = "error"
        record["error"] = f"{type(error).__name__}: {error}"
    finally:
        record["finished_at"] = dt.datetime.now(dt.UTC).isoformat(timespec="seconds")
        record["host_end"] = host_state()
        write_record(record_path, record)
        if not keep_worktree:
            try:
                remove_worktree(clone, worktree)
                record["pool_prune"] = prune_pool(root, drop_revisions=(failure.base_sha,))
            except Exception as error:
                record["cleanup_error"] = str(error)
            write_record(record_path, record)
    return record


def remeasure(
    failure: Failure, *, root: Path = DEFAULT_ROOT, clone: Path = DEFAULT_CLONE, build_timeout: float = 3 * 3600
) -> dict:
    """Measure again from the retained result ref after a measurement failure, without a new agent run."""
    experiment_dir = root / str(failure.candidate_id)
    record_path = experiment_dir / "result.json"
    record = json.loads(record_path.read_text())
    worktree = root / "worktrees" / f"base-{failure.base_sha[:12]}"
    seed_packages = root / "seed" / "packages"
    result_tip = git(clone, "rev-parse", "--verify", f"{REF_PREFIX}/result/{failure.ref}").strip()
    chain_before = read_chain(clone, failure.base_sha, record["sources"]["chain_tip"])
    env = lake_env(root)
    record["remeasured"] = (record.get("remeasured") or 0) + 1
    record.pop("error", None)
    if record.get("agent"):
        record["agent"].update(agent_usage(Path(record["agent"]["stream"])))
    try:
        checkout_worktree(clone, result_tip, worktree)
        link_shared_packages(worktree, seed_packages)
        record["result"] = measure_repair(clone, worktree, failure, chain_before, env, experiment_dir, build_timeout)
        record["status"] = "complete"
    except Exception as error:
        record["status"] = "error"
        record["error"] = f"{type(error).__name__}: {error}"
    record["finished_at"] = dt.datetime.now(dt.UTC).isoformat(timespec="seconds")
    record["host_end"] = host_state()
    write_record(record_path, record)
    return record


# ----------------------------------------------------------------------------------------------
# Report


def load_results(root: Path) -> list[dict]:
    results = []
    for path in sorted(root.glob("*/result.json")):
        try:
            results.append(json.loads(path.read_text()))
        except json.JSONDecodeError:
            continue
    results.sort(key=lambda r: r["candidate"]["candidate_id"])
    return results


def _minutes(seconds) -> str:
    return "-" if seconds is None else f"{seconds / 60:.1f}"


def result_row(record: dict) -> dict:
    """The flat per-candidate record committed to docs/queue/repair-experiments.json."""
    candidate = record["candidate"]
    sources = record.get("sources") or {}
    artifacts = record.get("artifacts") or {}
    build = artifacts.get("incremental_build") or {}
    agent = record.get("agent") or {}
    result = record.get("result") or {}
    confirm = result.get("confirm_build") or {}
    tokens = agent.get("tokens") or {}
    failing = candidate.get("failing_modules")
    if not failing and sources.get("ci_findings"):
        failing = findings_modules(sources["ci_findings"])
    warmth = None
    if build:
        warmth = {
            "base_cache_seconds": (artifacts.get("base_cache") or {}).get("seconds"),
            "base_mapping_found": (artifacts.get("base_cache") or {}).get("mapping_found"),
            "candidate_outputs_published": (artifacts.get("candidate_outputs_published") or {}).get("published"),
            "modules_built": build.get("built"),
            "modules_replayed": build.get("replayed"),
            "modules_fetched": build.get("fetched"),
            "failed_modules_local": build.get("failed_modules"),
            "build_seconds": build.get("seconds"),
            "build_rc": build.get("rc"),
            "load_1m_at_build": (build.get("load_before") or {}).get("load_1m"),
        }
    time_to_green = None
    if result.get("green") and agent.get("wall_seconds") is not None and confirm.get("seconds") is not None:
        time_to_green = round(agent["wall_seconds"] + confirm["seconds"], 1)
    return {
        "candidate": candidate["candidate_id"],
        "generation": candidate["generation"],
        "batch": candidate["batch_id"],
        "lineage": candidate.get("lineage"),
        "members": len(candidate["members"]),
        "base_sha": candidate["base_sha"][:12],
        "kinds": candidate.get("kinds")
        or (result.get("kinds") if result else None)
        or _kinds_from_findings(sources.get("ci_findings")),
        "failing_modules": failing,
        "suspects": candidate.get("suspects"),
        "status": record.get("status"),
        "error": record.get("error"),
        "ci_modules_built": ((sources.get("ci_findings") or {}).get("built_modules")),
        "preparation": warmth,
        "preparation_seconds": artifacts.get("seconds"),
        "chain_source": sources.get("chain_source"),
        "chain_tool_tree_matches_retained": (sources.get("chain_tool") or {}).get("tree_matches_retained"),
        "chain_tree_matches_retained": sources.get("chain_tree_matches_retained"),
        "worktree_reused": sources.get("worktree_reused"),
        "agent_wall_seconds": agent.get("wall_seconds"),
        "agent_rc": agent.get("rc"),
        "agent_timed_out": agent.get("timed_out"),
        "agent_turns": agent.get("num_turns"),
        "model": agent.get("model"),
        "tokens": tokens,
        "estimated_cost_usd": agent.get("estimated_cost_usd"),
        "claude_reported_cost_usd": agent.get("claude_reported_cost_usd"),
        "green": result.get("green"),
        "confirm_build_seconds": confirm.get("seconds"),
        "confirm_modules_built": confirm.get("built"),
        "confirm_failed_modules": confirm.get("failed_modules"),
        "confirm_lint_rc": (result.get("confirm_lint") or {}).get("rc"),
        "time_to_green_seconds": time_to_green,
        "edited_prs": result.get("edited_prs"),
        "shrink_prs": result.get("shrink_prs"),
        "emptied_prs": result.get("emptied_prs"),
        "messages_preserved": result.get("messages_preserved"),
        "edited_without_explanation": result.get("edited_without_explanation"),
        "validation_problems": result.get("validation_problems"),
        "diffstat": result.get("diffstat"),
        "load_1m_at_agent_start": (agent.get("load_before") or {}).get("load_1m"),
        "load_1m_at_agent_end": (agent.get("load_after") or {}).get("load_1m"),
        "disk_free_gb_start": (record.get("host_start") or {}).get("disk_free_gb"),
        "started_at": record.get("started_at"),
        "finished_at": record.get("finished_at"),
    }


def results_table(rows: list[dict]) -> str:
    header = (
        "| candidate | lineage | members | kind | failing modules | prep (min) | cache warmth | agent (min) | green | "
        "edited PRs | shrink | tokens | est. USD |"
    )
    lines = [header, "| ---: | ---: | ---: | --- | --- | ---: | --- | ---: | --- | --- | --- | ---: | ---: |"]
    for row in rows:
        prep = row.get("preparation") or {}
        warmth = "-"
        if prep:
            warmth = f"{prep.get('modules_built') or 0} built"
            if prep.get("modules_replayed"):
                warmth += f", {prep['modules_replayed']} replayed"
            if prep.get("base_mapping_found") is False:
                warmth += ", no base mapping"
        green = "-" if row.get("green") is None else ("yes" if row["green"] else "no")
        if str(row.get("status") or "").startswith("aborted"):
            green = "aborted"
        if row.get("agent_timed_out"):
            green += " (agent timed out)"
        tokens = (row.get("tokens") or {}).get("total_tokens")
        cost = row.get("estimated_cost_usd")
        modules = ", ".join(Path(m).stem for m in (row.get("failing_modules") or [])) or "-"
        lines.append(
            f"| {row['candidate']} | {row.get('lineage')} | {row['members']} | {'+'.join(row.get('kinds') or []) or '?'} | {modules} | "
            f"{_minutes(row.get('preparation_seconds'))} | {warmth} | {_minutes(row.get('agent_wall_seconds'))} | {green} | "
            f"{', '.join('#' + str(p) for p in row.get('edited_prs') or []) or '-'} | "
            f"{', '.join('#' + str(p) for p in row.get('shrink_prs') or []) or '-'} | "
            f"{tokens if tokens is not None else '-'} | {f'{cost:.2f}' if cost is not None else '-'} |"
        )
    return "\n".join(lines)


def distribution(values: list[float]) -> dict:
    if not values:
        return {"count": 0}
    ordered = sorted(values)

    def pct(fraction: float) -> float:
        index = min(len(ordered) - 1, max(0, round(fraction * (len(ordered) - 1))))
        return ordered[index]

    return {
        "count": len(ordered),
        "min": ordered[0],
        "p25": pct(0.25),
        "median": pct(0.5),
        "p75": pct(0.75),
        "max": ordered[-1],
        "mean": round(sum(ordered) / len(ordered), 1),
    }


def summary(rows: list[dict]) -> dict:
    complete = [r for r in rows if r.get("status") == "complete"]
    green = [r for r in complete if r.get("green")]
    return {
        "candidates": len(rows),
        "complete": len(complete),
        "green": len(green),
        "lineages": len({r.get("lineage") for r in rows}),
        "agent_minutes": distribution(
            [round(r["agent_wall_seconds"] / 60, 1) for r in complete if r.get("agent_wall_seconds") is not None]
        ),
        "time_to_green_minutes": distribution(
            [round(r["time_to_green_seconds"] / 60, 1) for r in green if r.get("time_to_green_seconds") is not None]
        ),
        "preparation_minutes": distribution(
            [round(r["preparation_seconds"] / 60, 1) for r in rows if r.get("preparation_seconds") is not None]
        ),
        "modules_built_in_preparation": distribution(
            [
                r["preparation"]["modules_built"]
                for r in rows
                if (r.get("preparation") or {}).get("modules_built") is not None
            ]
        ),
        "estimated_cost_usd_total": round(sum(r["estimated_cost_usd"] or 0 for r in complete), 2),
        "total_tokens": sum((r.get("tokens") or {}).get("total_tokens") or 0 for r in complete),
        "shrink_events": sum(len(r.get("shrink_prs") or []) for r in complete),
        "messages_preserved_everywhere": all(r.get("messages_preserved") is not False for r in complete),
    }


DOC_START = "<!-- results-table -->"
DOC_END = "<!-- /results-table -->"


def _dist_line(label: str, dist: dict) -> str:
    if not dist.get("count"):
        return f"- {label}: no data"
    return (
        f"- {label} (n={dist['count']}): min {dist['min']}, p25 {dist['p25']}, median {dist['median']}, "
        f"p75 {dist['p75']}, max {dist['max']}, mean {dist['mean']}"
    )


def results_section(rows: list[dict], summary_: dict) -> str:
    """The generated part of docs/queue/repair-experiments.md: the table and the distributions."""
    lines = [
        f"{summary_['candidates']} candidates in {summary_['lineages']} lineages; {summary_['complete']} complete, "
        f"{summary_['green']} green. Total {summary_['total_tokens']:,} tokens, estimated USD "
        f"{summary_['estimated_cost_usd_total']:.2f} at API list prices; {summary_['shrink_events']} shrink events; "
        f"messages preserved as prefixes everywhere: {'yes' if summary_['messages_preserved_everywhere'] else 'no'}.",
        "",
        results_table(rows),
        "",
        "Minutes:",
        "",
        _dist_line("agent wall time, handoff to exit", summary_["agent_minutes"]),
        _dist_line("time to green (agent plus confirming build)", summary_["time_to_green_minutes"]),
        _dist_line("preparation (base cache fetch plus incremental build)", summary_["preparation_minutes"]),
        _dist_line("modules built in preparation", summary_["modules_built_in_preparation"]),
    ]
    return "\n".join(lines)


def update_document(path: Path, rows: list[dict], summary_: dict) -> None:
    text = path.read_text()
    if DOC_START not in text or DOC_END not in text:
        raise SystemExit(f"{path} has no {DOC_START} ... {DOC_END} markers")
    head, rest = text.split(DOC_START, 1)
    _, tail = rest.split(DOC_END, 1)
    path.write_text(head + DOC_START + "\n" + results_section(rows, summary_) + "\n" + DOC_END + tail)


# ----------------------------------------------------------------------------------------------
# CLI


def _journal_paths(args) -> list[Path]:
    if args.journal:
        return [Path(p) for p in args.journal]
    default = sorted(
        Path(os.environ.get("TAUCETI_BORS_JOURNAL_DIR") or Path.home() / "tmp" / "bors-journal").glob(
            "candidates-*.json"
        )
    )
    if not default:
        raise SystemExit("no journal snapshots: pass --journal")
    return default


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m tauceti_queue.repair_experiment", description=__doc__.split("\n\n")[0]
    )
    sub = parser.add_subparsers(dest="command", required=True)
    select = sub.add_parser("select", help="list eligible failed candidates and their lineages")
    select.add_argument("--journal", action="append", help="candidate journal snapshot (repeatable)")
    select.add_argument(
        "--include",
        type=int,
        action="append",
        default=[],
        help="force a candidate whose journal diagnostics are incomplete",
    )
    select.add_argument("--json", action="store_true")
    run = sub.add_parser("run", help="run one experiment")
    run.add_argument("--candidate", type=int, required=True)
    run.add_argument("--journal", action="append")
    run.add_argument("--include", type=int, action="append", default=[])
    run.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    run.add_argument("--clone", type=Path, default=DEFAULT_CLONE)
    run.add_argument("--agent-timeout", type=float, default=3 * 3600)
    run.add_argument("--build-timeout", type=float, default=3 * 3600)
    run.add_argument("--skip-agent", action="store_true", help="prepare and measure artifacts only")
    run.add_argument("--remove-worktree", action="store_true", help="remove the base worktree afterwards")
    run.add_argument("--min-free-gb", type=float, default=25.0, help="required free disk before and after preparation")
    clean = sub.add_parser("clean", help="remove every experiment worktree under the root and prune the pool")
    clean.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    clean.add_argument("--clone", type=Path, default=DEFAULT_CLONE)
    prune = sub.add_parser("prune", help="drop built outputs from the private artifact pool")
    prune.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    measure = sub.add_parser("measure", help="measure a finished agent run again from its retained result ref")
    measure.add_argument("--candidate", type=int, required=True)
    measure.add_argument("--journal", action="append")
    measure.add_argument("--include", type=int, action="append", default=[])
    measure.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    measure.add_argument("--clone", type=Path, default=DEFAULT_CLONE)
    measure.add_argument("--build-timeout", type=float, default=3 * 3600)
    report = sub.add_parser("report", help="aggregate result records into the results table and JSON")
    report.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    report.add_argument("--json", type=Path, help="write the per-candidate records here")
    report.add_argument("--markdown", action="store_true", help="print the results table and summary")
    report.add_argument("--doc", type=Path, help="rewrite the results section of this document between its markers")
    args = parser.parse_args(argv)

    if args.command == "select":
        journal = load_journal(_journal_paths(args))
        failures = select_candidates(journal, tuple(args.include))
        if args.json:
            print(json.dumps([f.to_json() for f in failures], indent=1))
        else:
            print(selection_table(failures))
            print()
            for root, ids in lineages(failures).items():
                print(f"lineage {root}: {', '.join(map(str, ids))}")
            print(f"\n{len(failures)} candidates in {len(lineages(failures))} lineages")
        return 0
    if args.command == "run":
        journal = load_journal(_journal_paths(args))
        failures = {f.candidate_id: f for f in select_candidates(journal, tuple(args.include) + (args.candidate,))}
        failure = failures.get(args.candidate)
        if failure is None:
            raise SystemExit(f"candidate {args.candidate} is not an eligible failure in the journal")
        record = run_experiment(
            failure,
            root=args.root,
            clone=args.clone,
            agent_timeout=args.agent_timeout,
            build_timeout=args.build_timeout,
            skip_agent=args.skip_agent,
            keep_worktree=not args.remove_worktree,
            min_free_gb=args.min_free_gb,
        )
        print(json.dumps(result_row(record), indent=1, default=str))
        return 0 if record.get("status") in {"complete", "prepared_only"} else 1
    if args.command == "measure":
        journal = load_journal(_journal_paths(args))
        failures = {f.candidate_id: f for f in select_candidates(journal, tuple(args.include) + (args.candidate,))}
        failure = failures.get(args.candidate)
        if failure is None:
            raise SystemExit(f"candidate {args.candidate} is not an eligible failure in the journal")
        record = remeasure(failure, root=args.root, clone=args.clone, build_timeout=args.build_timeout)
        print(json.dumps(result_row(record), indent=1, default=str))
        return 0 if record.get("status") == "complete" else 1
    if args.command == "clean":
        for path in sorted((args.root / "worktrees").glob("*")):
            remove_worktree(args.clone, path)
            print(f"removed {path}")
        print(json.dumps(prune_pool(args.root)))
        return 0
    if args.command == "prune":
        print(json.dumps(prune_pool(args.root)))
        return 0
    if args.command == "report":
        rows = [result_row(r) for r in load_results(args.root)]
        payload = {
            "schema": "tauceti-queue.repair-experiments/v1",
            "generated_at": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
            "summary": summary(rows),
            "candidates": rows,
        }
        if args.json:
            args.json.parent.mkdir(parents=True, exist_ok=True)
            args.json.write_text(json.dumps(payload, indent=1) + "\n")
        if args.doc:
            update_document(args.doc, rows, payload["summary"])
        if args.markdown or not (args.json or args.doc):
            print(results_table(rows))
            print()
            print(json.dumps(payload["summary"], indent=1))
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())

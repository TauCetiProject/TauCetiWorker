"""Discover both inputs, then execute one unit on its originating forge."""

from __future__ import annotations

import dataclasses
import random

from tauceti_tools.forge import ForgeError

from .config import Die, NoProgress, log, roadmap_only
from .constants import AUTO_STAGES
from .review_state import ReviewState
from .survey import Counters, Survey, survey
from .worker_sources import activate, native_source, source_config


def collect(w, opts, *, use_fleet_focus=True):
    import os

    from .github_state import enabled as github_state_enabled
    from .github_transport import read_context
    from .source_claims import SourceClaims
    from .work_units import Worker, round_scope, want

    stages = [stage for stage in AUTO_STAGES if want(opts.only, stage)]
    scope, global_pick, focused_round = round_scope(w, opts, use_fleet_focus)
    scope = "auto" if scope is None else scope
    result = []
    if os.environ.get("TAUCETI_WORKER_FORGES") != "forgejo":
        try:
            with read_context(retry=False):
                github = survey(
                    w.cfg,
                    w.gh,
                    w.rs,
                    w.counters,
                    deep=True,
                    stages=stages,
                    selected_roadmap=scope,
                    numbers=opts.prs,
                    count_all_roadmaps=global_pick or focused_round,
                    cached_only=not opts.prs and scope in ("auto", "any") and github_state_enabled(),
                )
        except (Die, ForgeError, OSError, RuntimeError) as error:
            github = Survey(worker_id=w.cfg.wid, github_failed=True)
            github.errors.append(f"GitHub discovery unavailable ({type(error).__name__})")
        from tauceti_tools.forge import GITHUB, RepositoryRef

        reference = RepositoryRef(GITHUB, w.gh.repo)
        for stage in ("rebase", "review", "fix", "fix-ci", "bump", "lint-repair"):
            kind = github.kind(stage)
            for candidate in kind.actionable + kind.suppressed:
                candidate.source_repository = reference
        result.append(("github", w, github))
    try:
        source = native_source(w.cfg)
        cfg = source_config(w.cfg, source)
        worker = Worker(cfg, source, ReviewState(cfg, source), Counters(cfg), w.rc, SourceClaims(cfg, w.rc, source))
        native = survey(
            cfg,
            source,
            worker.rs,
            worker.counters,
            deep=True,
            stages=stages,
            selected_roadmap=scope,
            numbers=opts.prs,
            count_all_roadmaps=global_pick or focused_round,
        )
        result.append(("forgejo", worker, native))
    except (Die, ForgeError, OSError, ValueError) as error:
        unavailable = Survey(worker_id=w.cfg.wid, github_failed=True)
        unavailable.errors.append(f"Forgejo discovery unavailable ({type(error).__name__})")
        result.append(("forgejo", None, unavailable))
    for name, worker, observation in result:
        if name == "github" and not observation.github_failed and github_state_enabled():
            enrich_targets(worker, observation)
        observation._round_scope = (global_pick, focused_round)
    return result


def enrich_targets(worker, observation):
    """Compact survey rows omit bodies; reuse the existing cached inventory."""
    if not observation.open_prs:
        return
    from .github_state import read
    from .github_transport import read_context
    from .survey import origin_pr_url, work_target_keys

    try:
        with read_context(retry=False):
            available, rows = read(worker.gh.repo, "open_prs", _optional=True)
    except (Die, ForgeError, OSError):
        available, rows = False, None
    bodies = {
        (row["number"], row["headRefOid"]): row["body"]
        for row in rows or []
        if isinstance(row, dict)
        and isinstance(row.get("body"), str)
        and type(row.get("number")) is int
        and isinstance(row.get("headRefOid"), str)
    }
    observation.target_inventory_available = available and all(
        (pr.number, pr.head_oid) in bodies for pr in observation.open_prs
    )
    if not observation.target_inventory_available:
        observation.errors.append(
            "GitHub target-marker inventory unavailable; new authoring waits for complete target coverage"
        )
    observation.open_prs = [
        dataclasses.replace(
            pr,
            work_targets=work_target_keys(bodies[(pr.number, pr.head_oid)]),
            origin_pr=origin_pr_url(bodies[(pr.number, pr.head_oid)]),
        )
        if (pr.number, pr.head_oid) in bodies
        else pr
        for pr in observation.open_prs
    ]


def deduplicate(observations):
    """Prefer the existing GitHub proposal when the same head appears on both."""
    from tauceti_tools.forge import GITHUB, RepositoryRef

    known = []
    for name, worker, result in observations:
        if result.github_failed or worker is None:
            continue
        repository = worker.gh.repository if name == "forgejo" else RepositoryRef(GITHUB, worker.gh.repo)
        for pr in result.open_prs:
            known.append((name, pr, repository.pull_url(pr.number)))
    github = [pr for name, pr, _ in known if name == "github"]
    github_heads = {pr.head_oid for pr in github}
    github_targets = {target for pr in github for target in pr.work_targets}
    github_urls = {url for name, _, url in known if name == "github"}
    native_origins = {pr.origin_pr for pr in github if pr.origin_pr}
    for name, _, result in observations:
        duplicates = set()
        for pr in result.open_prs:
            if name == "forgejo" and (
                pr.head_oid in github_heads
                or bool(set(pr.work_targets) & github_targets)
                or pr.origin_pr in github_urls
            ):
                # An explicit GitHub copy names the original Forgejo PR. Keep
                # scheduling that original even when its head is also on GitHub.
                source = next(
                    (url for forge, candidate, url in known if forge == name and candidate.number == pr.number), ""
                )
                if source not in native_origins:
                    duplicates.add(pr.number)
            if name == "github" and pr.origin_pr in {url for forge, _, url in known if forge == "forgejo"}:
                duplicates.add(pr.number)
        for stage in ("rebase", "review", "fix", "fix-ci", "bump", "lint-repair"):
            kind = result.kind(stage)
            kind.actionable = [candidate for candidate in kind.actionable if candidate.pr not in duplicates]


def run_round(w, opts, *, use_fleet_focus=True):
    import os

    from .work_units import NoEligibleWork, note_spent_budgets, want
    from .work_units import run_round as ordinary_round

    observations = collect(w, opts, use_fleet_focus=use_fleet_focus)
    deduplicate(observations)
    for name, _, result in observations:
        if result.github_failed or result.dependency_wait:
            log(f"{name}: unavailable; " + "; ".join(result.errors))
        else:
            log(f"{name}: {result.n_open_nondraft} open PRs; {result.status_label_line()}")
            for error in result.errors:
                log(error)
    healthy = [
        (name, worker, result)
        for name, worker, result in observations
        if worker is not None and not result.github_failed and not result.dependency_wait
    ]
    from .config import SharedStateUnavailable

    waits = []
    for name, _, result in observations:
        if result.dependency_wait:
            if result.dependency_wait.get("actionable"):
                waits.insert(0, Die("; ".join(result.errors) + " — source configuration requires operator action"))
            else:
                waits.append(
                    SharedStateUnavailable("; ".join(result.errors) or f"{name} unavailable", result.dependency_wait)
                )
    for _, worker, result in healthy:
        note_spent_budgets(worker, result, opts)
    if not healthy:
        if waits:
            raise waits[0]
        raise NoProgress("all selected source surveys are unavailable")
    if want(opts.only, "curate") and not getattr(opts, "prs", ()):
        # The target list is curated against GitHub, once a round and ahead of any source's work.
        github = next(((worker, result) for name, worker, result in healthy if name == "github"), None)
        if github is not None:
            try:
                return ordinary_round(github[0], dataclasses.replace(opts, only=["curate"]), prepared_survey=github[1])
            except NoProgress as error:
                if opts.only == ["curate"]:
                    raise
                log(f"{error}; going on to the other selected work")
        elif opts.only == ["curate"]:
            raise NoProgress("curate: GitHub was not surveyed this round, and the target list is curated against it")
        opts = dataclasses.replace(opts, only=[task for task in opts.only if task != "curate"])
    executable = [(name, worker, result) for name, worker, result in healthy if name == "github" or opts.sandbox_host]
    if len(executable) != len(healthy):
        log("Forgejo agent rounds currently require host mode")
    random.shuffle(executable)
    from .config import ClaimCollision, GitHubUnavailable, SourceUnavailable
    from .survey import bump_prs

    stages = [stage for stage in AUTO_STAGES if want(opts.only, stage)]
    choices = []
    for name, worker, result in executable:
        bumps = bump_prs(result)
        for index, stage in enumerate(stages):
            candidates = result.kind(stage).actionable
            if candidates:
                priority = 0 if any(candidate.pr in bumps for candidate in candidates) else 1
                choices.append((priority, index, name, worker, result, stage))
    complete = all(
        not value.github_failed and not value.dependency_wait and getattr(value, "target_inventory_available", True)
        for _, _, value in observations
    )
    execution_failed = set()
    collisions = False
    for _, _, name, worker, result, stage in sorted(choices, key=lambda item: item[:2]):
        if name in execution_failed:
            continue
        selected = dataclasses.replace(opts, only=[stage])
        try:
            if name == "github":
                return ordinary_round(worker, selected, prepared_survey=result)
            with activate(worker.gh, worker.cfg):
                return ordinary_round(worker, selected, prepared_survey=result)
        except (ClaimCollision, GitHubUnavailable, SourceUnavailable, NoEligibleWork, ForgeError) as error:
            if isinstance(error, GitHubUnavailable):
                waits.append(error)
            collisions |= isinstance(error, ClaimCollision)
            if not isinstance(error, (ClaimCollision, NoEligibleWork)):
                execution_failed.add(name)
            log(f"{name}: selected work deferred ({type(error).__name__}); checking the other source")
            continue
    destination = os.environ.get("TAUCETI_NEW_PR_FORGE", "github")
    # Partial discovery cannot prove that a roadmap target is unclaimed on the
    # other source. Existing PR review/repair above remains available.
    inventory = [
        {"source": name, "pr": pr.number, "head": pr.head_oid, "targets": pr.work_targets}
        for name, _, result in healthy
        for pr in result.open_prs
        if pr.work_targets
    ]
    authored = sum(result.n_mine_open for _, _, result in healthy)
    for name, worker, result in executable:
        if (
            name == destination
            and name not in execution_failed
            and complete
            and authored < result.max_open_prs
            and want(opts.only, "roadmap")
            and not opts.prs
        ):
            worker.cross_source_targets = inventory
            selected = dataclasses.replace(opts, only=["roadmap"])
            if name == "github":
                return ordinary_round(worker, selected, prepared_survey=result)
            with activate(worker.gh, worker.cfg):
                return ordinary_round(worker, selected, prepared_survey=result)
    if (
        use_fleet_focus
        and want(opts.only, "roadmap")
        and any(getattr(result, "_round_scope", (False, False))[1] for _, _, result in healthy)
    ):
        log("Fleet roadmap focus: no available focused work; selecting global maintenance")
        return run_round(w, opts, use_fleet_focus=False)
    if waits:
        raise waits[0]
    error = ClaimCollision if collisions else NoEligibleWork
    raise error("no eligible PR work on the selected forges")


def display_survey(cfg, gh):
    """The dashboard shares discovery with the worker and preserves source labels."""
    from .work_units import RoundOpts, Worker

    worker = Worker(cfg, gh, ReviewState(cfg, gh), Counters(cfg), None, None)
    options = RoundOpts(only=[], agent="auto", work_model="auto", sandbox_host=True, dry_run=True)
    observations = collect(worker, options)
    deduplicate(observations)
    result = Survey(worker_id=cfg.wid, roadmap_only=roadmap_only() or "auto")
    result.github_failed = all(value.github_failed for _, _, value in observations)
    result.source_states = {}
    for name, _, value in observations:
        result.source_states[name] = "unavailable" if value.github_failed else "available"
        result.errors.extend(f"{name}: {error}" for error in value.errors)
        result.open_prs.extend(value.open_prs)
        result._mine_open_prs.extend(value._mine_open_prs)
        result.n_open_nondraft += value.n_open_nondraft
        result.n_mine_open += value.n_mine_open
        for stage in (
            "rebase",
            "review",
            "fix",
            "fix-ci",
            "bump",
            "lint-repair",
            "progress",
            "overlap-scan",
            "overlap-fix",
        ):
            result.kind(stage).actionable.extend(value.kind(stage).actionable)
            result.kind(stage).suppressed.extend(value.kind(stage).suppressed)
    from .constants import STATUS_LABELS

    result.status_labels = [
        (
            label,
            sum(total for _, _, value in observations for key, total, _ in value.status_labels if key == label),
            sum(mine for _, _, value in observations for key, _, mine in value.status_labels if key == label),
        )
        for label in STATUS_LABELS
    ]
    result.n_status_unlabeled = sum(value.n_status_unlabeled for _, _, value in observations)
    result.roadmap_backpressure = result.n_mine_open >= result.max_open_prs
    result.next_auto_stage = next((stage for stage in AUTO_STAGES if result.kind(stage).actionable), None)
    if result.next_auto_stage is None and not result.roadmap_backpressure and not result.errors:
        result.next_auto_stage = "roadmap"
    return result


def recover_native(cfg, ctx, opts):
    """Reconcile retained native publication before model admission."""
    from .repair_recovery import recover
    from .source_claims import SourceClaims
    from .source_publication import pending
    from .work_units import Worker

    source = native_source(cfg)
    cfg = source_config(cfg, source)
    if source.anonymous_discovery:
        return False
    with activate(source, cfg):
        worker = Worker(cfg, source, ReviewState(cfg, source), Counters(cfg), ctx, SourceClaims(cfg, ctx, source))
        return pending(source, cfg, numbers=getattr(opts, "prs", ())) or recover(worker, opts)

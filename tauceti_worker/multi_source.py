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


def collect(w, opts, *, use_fleet_focus=True, display=False):
    import os

    from .github_state import enabled as github_state_enabled
    from .github_transport import read_context
    from .source_claims import SourceClaims
    from .work_units import Worker, round_scope, want

    stages = [stage for stage in AUTO_STAGES if want(opts.only, stage)]
    scope, global_pick, focused_round = round_scope(w, opts, use_fleet_focus, display=display)
    scope = "auto" if scope is None else scope
    umbrella = getattr(w, "umbrella", None)
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
                    umbrella=umbrella,
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
            umbrella=umbrella,
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

    from .work_units import NoEligibleWork, want
    from .work_units import run_round as ordinary_round

    if use_fleet_focus and isinstance(getattr(opts, "unavailable_sources", None), list):
        # One outage cutoff across every source and landing tier; the unfocused fallback keeps it.
        opts.unavailable_sources.clear()
    observations = collect(w, opts, use_fleet_focus=use_fleet_focus)
    deduplicate(observations)
    for name, _, result in observations:
        if result.github_failed or result.dependency_wait:
            log(f"{name}: unavailable; " + "; ".join(result.errors))
        else:
            log(f"{name}: {result.n_open_nondraft} open PRs; {result.status_label_line()}")
            if result.detail_reads or result.detail_deferred:
                log(
                    f"{name}: review index refreshed with {result.detail_reads} read(s); "
                    f"{result.detail_deferred} PR(s) left unrefreshed for the next survey"
                )
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
    if not healthy:
        if waits:
            raise waits[0]
        raise NoProgress("all selected source surveys are unavailable")
    from .account_pressure import count_account, report

    # Every healthy survey judges authoring against the account's open PRs on all project forges;
    # a forge this worker does not survey is read, and a failed survey's forge counts as unknown.
    unknown_forges = {name for name, _, value in observations if value.github_failed or value.dependency_wait}
    account = count_account(w, {name: result for name, _, result in healthy}, unknown=unknown_forges)
    report(account, healthy[0][2])
    executable = [(name, worker, result) for name, worker, result in healthy if name == "github" or opts.sandbox_host]
    if len(executable) != len(healthy):
        log("Forgejo agent rounds currently require host mode")
    random.shuffle(executable)
    from .config import ClaimCollision, GitHubUnavailable, SourceUnavailable
    from .survey import LANDING_TIERS, bump_prs, landing_tier, review_first_rank

    stages = [stage for stage in AUTO_STAGES if want(opts.only, stage)]
    # A review-first worker (--review-first) offers re-reviews, then first reviews, on either forge
    # before any other PR work, and falls back to the landing order below when no review is
    # claimable. Automatic bumps still come first and overlap work last.
    review_first = os.environ.get("TAUCETI_REVIEW_FIRST") == "1"
    # While GitHub rejects an identity's content writes, a new GitHub review it would publish would
    # do its model work and then sleep in the publication pacer, holding this slot for up to an hour.
    # Offer those reviews after every other choice instead, so the slot repairs PRs or reviews on
    # Forgejo meanwhile. Each PR publishes as the account or the review App (review_app.routes), and
    # each has its own rejection window, so only the backed-off identity's reviews wait. Automatic
    # bumps keep their lead; with nothing else claimable the review still runs.
    held = None
    if any(name == "github" for name, _, _ in executable) and want(opts.only, "review"):
        from . import review_app
        from .github_transport import publication_backoff

        backoff = {False: publication_backoff(), True: review_app.publication_backoff()}
        for app, seconds in backoff.items():
            if seconds:
                who = review_app.app_login() if app else "the account"
                log(f"github: publication backoff for {who}, {int(seconds)}s; offering its reviews after other work")
        if any(backoff.values()):

            def held(worker, pr):
                repository = getattr(worker.gh, "review_repository", worker.gh.repo)
                return backoff[review_app.routes(repository, pr)] > 0

    # One choice per (landing tier, source), walked tier by tier across sources: a re-review on
    # either forge is offered before any fix, a fix before any first review (survey.LANDING_TIERS).
    choices = []
    for name, worker, result in executable:
        bumps = bump_prs(result)
        waiting = set()
        if held and name == "github":
            waiting = {
                c.pr
                for c in result.reviewable.actionable
                if landing_tier("review", c, bumps) > 0 and held(worker, c.pr)
            }
        publishable, unpublishable = result, None
        if waiting:
            # The review throttles judge the whole queue, so apply them before splitting it, as
            # ordinary_round would have once, and recompute what is left to wait.
            if not getattr(result, "_review_throttle_applied", False):
                from .work_units import throttle_review

                throttle_review(result, opts)
                result._review_throttle_applied = True
            waiting = {c.pr for c in result.reviewable.actionable} & waiting
        if waiting:
            publishable, unpublishable = (
                _with_reviews(result, [c for c in result.reviewable.actionable if c.pr not in keep])
                for keep in (waiting, {c.pr for c in result.reviewable.actionable} - waiting)
            )
        tiers = {}
        for stage in stages:
            for candidate in result.kind(stage).actionable:
                tiers.setdefault(landing_tier(stage, candidate, bumps), []).append(stage)
        for tier, tier_stages in tiers.items():
            tier_stages = list(dict.fromkeys(tier_stages))
            if waiting and "review" in tier_stages:
                reviews = [c.pr for c in result.reviewable.actionable if landing_tier("review", c, bumps) == tier]
                if any(pr in waiting for pr in reviews):
                    choices.append((tier, name, worker, unpublishable, ["review"], False, True))
                    if all(pr in waiting for pr in reviews):
                        tier_stages = [stage for stage in tier_stages if stage != "review"]
                        if not tier_stages:
                            continue
            if review_first and tier > 0 and "review" in tier_stages and not review_first_rank("review", tier):
                choices.append((tier, name, worker, publishable, ["review"], True, False))
                tier_stages = [stage for stage in tier_stages if stage != "review"]
                if not tier_stages:
                    continue
            choices.append((tier, name, worker, publishable, tier_stages, False, False))
    complete = all(
        not value.github_failed and not value.dependency_wait and getattr(value, "target_inventory_available", True)
        for _, _, value in observations
    )
    execution_failed = set()
    collisions = False
    # A forge preference (--prefer-forge) walks the preferred forge's landing tiers (rebases and
    # re-reviews, fixes, first reviews) before the other forge's, so a preferring worker reaches its
    # forge's fixes even while the other forge has re-reviews left. Automatic bumps still come first
    # on either forge (main outruns them), and overlap work still comes last on either forge.
    preferred = os.environ.get("TAUCETI_PREFER_FORGE")

    def order(item):
        tier, name, reviewing, unpublishable = item[0], item[1], item[5], item[6]
        band = 0 if tier == 0 else 2 if tier >= LANDING_TIERS["overlap-fix"] else 1
        # Review-first choices go re-reviews then first reviews; the preference orders forges inside each.
        return (
            unpublishable,
            band,
            not reviewing,
            tier if reviewing else 0,
            band == 1 and bool(preferred) and name != preferred,
            tier,
        )

    for tier, name, worker, result, tier_stages, *_ in sorted(choices, key=order):
        if name in execution_failed:
            continue
        selected = dataclasses.replace(opts, only=tier_stages, landing_tier=tier)
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
    authored = sum(count for count in account.values() if count is not None)
    # An account count unknown on any project forge never reads as room to author.
    counts_known = None not in account.values()
    if not counts_known and want(opts.only, "roadmap") and not opts.prs:
        log(
            "Roadmap authoring waits: "
            + ", ".join(f"{name} open-PR count unavailable" for name, n in sorted(account.items()) if n is None)
        )
    for name, worker, result in executable:
        if (
            name == destination
            and name not in execution_failed
            and complete
            and counts_known
            and authored < result.max_open_prs
            and want(opts.only, "roadmap")
            and not opts.prs
        ):
            worker.cross_source_targets = inventory
            worker.target_sources = [other.gh for _, other, _ in healthy if other is not worker]
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
    observations = collect(worker, options, display=True)
    deduplicate(observations)
    result = Survey(worker_id=cfg.wid, roadmap_only=roadmap_only() or "auto")
    result._umbrella = worker.umbrella
    result.github_failed = all(value.github_failed for _, _, value in observations)
    result.source_states = {}
    for name, _, value in observations:
        result.source_states[name] = "unavailable" if value.github_failed else "available"
        result.errors.extend(f"{name}: {error}" for error in value.errors)
        result.open_prs.extend(value.open_prs)
        result._mine_open_prs.extend(value._mine_open_prs)
        result.n_open_nondraft += value.n_open_nondraft
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
    from .account_pressure import count_account

    failed = {name for name, _, value in observations if value.github_failed}
    healthy = {name: value for name, source, value in observations if source is not None and name not in failed}
    account = count_account(worker, healthy, unknown=failed) if healthy else {}
    # The combined survey holds every surveyed forge's own PRs; the dashboard's dials rescope those
    # and keep the other forges' counts, so an unknown one stays blocked. A Fleet-focused survey
    # counts every roadmap, which its focused rows do not hold: keep those totals as they are.
    if any(getattr(value, "_automatic_open_count", None) is not None for value in healthy.values()):
        result._automatic_open_count = sum(account[name] for name in healthy)
    result.count_account({name: count for name, count in account.items() if name not in healthy})
    result.next_auto_stage = next((stage for stage in AUTO_STAGES if result.kind(stage).actionable), None)
    if result.next_auto_stage is None and not result.roadmap_backpressure and not result.errors:
        result.next_auto_stage = "roadmap"
    return result


def publish_native_reviews(cfg, prs=()):
    """Retry publication of rendered native-forge review output before model admission."""
    from .source_publication import pending

    source = native_source(cfg)
    cfg = source_config(cfg, source)
    if source.anonymous_discovery:
        return False
    with activate(source, cfg):
        return pending(source, cfg, numbers=prs)


def _with_reviews(survey, reviews):
    """A shallow copy of `survey` whose review queue is `reviews`; other work kinds are shared."""
    import copy

    selected = copy.copy(survey)
    selected.reviewable = dataclasses.replace(survey.reviewable, actionable=list(reviews))
    return selected

"""Direct source inventory and qualified ownership in the ordinary survey consumer.

Native commit statuses are observations, not trusted execution evidence. The
inventory remains read-only; worker_survey adds native review and repair selection.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass

from tauceti_tools.forge import Incomplete, RepositoryRef

from .source_identity import SourceActor
from .survey import PRInfo, Survey, bucket_status_labels, completed_attempts, roadmap_open_count


@dataclass(frozen=True, kw_only=True)
class SourcePRInfo(PRInfo):
    source_repository: RepositoryRef
    source_author: SourceActor | None
    source_head_repository: RepositoryRef | None

    @property
    def automatic_bump(self) -> bool:
        from .constants import BUMP_HEAD_PREFIX

        return bool(
            self.author_is_bot
            and self.source_head_repository is not None
            and self.source_head_repository.key == self.source_repository.key
            and self.head_ref.startswith(BUMP_HEAD_PREFIX)
        )


@dataclass(kw_only=True)
class SourceSurvey(Survey):
    source_repository: RepositoryRef
    source_actor: SourceActor | None = None

    def rescope_roadmap(self) -> None:
        super().rescope_roadmap()
        self.next_auto_stage = None

    def owns(self, pr: PRInfo) -> bool:
        return bool(
            isinstance(pr, SourcePRInfo)
            and pr.source_repository.key == self.source_repository.key
            and self.source_actor is not None
            and pr.source_author == self.source_actor
        )

    def owns_author(self, author: dict | None) -> bool:
        if self.source_actor is None:
            return False
        try:
            return SourceActor.from_projection(self.source_repository.forge, author) == self.source_actor
        except Incomplete:
            return False


def source_pr_info(row, repository):
    # A caller cannot turn a native status named "build" into CI authority by
    # passing the ordinary gh-shaped projection through PRInfo.from_json.
    import re

    if (
        not isinstance(row, dict)
        or type(row.get("number")) is not int
        or row["number"] < 1
        or not isinstance(row.get("headRefOid"), str)
        or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", row["headRefOid"])
        or not isinstance(row.get("headRefName"), str)
        or not row["headRefName"]
    ):
        raise Incomplete("source inventory PR candidate is unavailable")
    base = PRInfo.from_json({**row, "statusCheckRollup": []})
    author = row.get("author")
    actor = None
    if isinstance(author, dict) and "id" in author:
        actor = SourceActor.from_projection(repository.forge, author)
    head = row.get("headRepository")
    head_ref = None if head is None else RepositoryRef(repository.forge, head.get("nameWithOwner"))
    return SourcePRInfo(
        **asdict(base), source_repository=repository, source_author=actor, source_head_repository=head_ref
    )


def inventory_survey(cfg, source, *, roadmap, skip, deep, stages, numbers, count_all_roadmaps):
    from .survey_facts import in_scope

    repository = source.repository
    if not isinstance(repository, RepositoryRef) or repository.forge.kind != "forgejo":
        raise ValueError("source survey requires an explicit Forgejo repository")
    result = SourceSurvey(worker_id=cfg.wid, roadmap_only=roadmap, roadmap_skip=skip, source_repository=repository)

    def unavailable(operation, failure=None):
        result.github_failed = True  # existing callers' dependency-failure gate
        result.dependency_wait = {
            "dependency": "source-forge",
            "reason": "unavailable",
            "operation": operation,
            "source": repository.key,
        }
        if failure is not None:
            result.dependency_wait.update(status=failure.status, kind=failure.kind)
        result.errors.append(f"source survey unavailable ({operation})")
        return result

    actor = source.authenticated_actor()
    if actor is None and getattr(source, "anonymous_discovery", False) is not True:
        return unavailable("authenticated_actor", source.last_failure)
    raw = source.open_prs()
    if raw is None:
        return unavailable("open_prs", source.last_failure)
    try:
        prs = [source_pr_info(row, repository) for row in raw]
    except (Incomplete, ValueError, TypeError, KeyError, AttributeError):
        return unavailable("inventory_projection")
    if not source.binding_current():
        return unavailable("binding", source.last_failure)
    result.source_actor, result.me_login = actor, actor.login if actor else ""
    if count_all_roadmaps:
        result._automatic_open_count = roadmap_open_count(
            [p for p in prs if not p.is_draft and result.owns(p)], "auto", skip
        )
    if deep:
        prs = [
            p
            for p in prs
            if (p.number in numbers if numbers else in_scope(p.labels, p.target_focuses, p.head_ref, roadmap, skip))
        ]
    result.open_prs = prs
    nondraft = [
        p
        for p in prs
        if not p.is_draft
        and not (
            p.automatic_bump
            and {label.lower() for label in p.labels} & {"keep", "hold", "wip", "human", "on-hold", "do-not-close"}
        )
    ]
    result.n_open_nondraft = len(nondraft)
    result._mine_open_prs = [p for p in nondraft if result.owns(p)]
    result.rescope_roadmap()
    result.status_labels, result.n_status_unlabeled = bucket_status_labels(nondraft, result.me_login, owns=result.owns)
    # rescope_roadmap also chooses authoring for legacy surveys. This inventory
    # supplies no source write, claim or execution authority, including authoring.
    result.next_auto_stage = None
    result.dependency_wait = {
        "dependency": "source-scheduling",
        "reason": "unsupported",
        "source": repository.key,
    }
    if deep and stages:
        result.github_failed = True
        result.errors.append("source scheduling requires verified CI, qualified claims and publication adapters")
    return result


def worker_survey(cfg, source, rs, counters, *, roadmap, skip, deep, stages, numbers, count_all_roadmaps):
    """Schedule review and owned-branch repair without asserting native CI authority."""
    from .constants import (
        MAX_FIX_ATTEMPTS,
        MAX_REBASE_ATTEMPTS,
        MAX_REVIEW_CONTESTS,
        MAX_REVIEW_CONTESTS_PER_RUBRIC,
        MAX_REVIEW_ERRORS,
    )
    from .survey import Candidate, _unchanged_review_recent

    result = inventory_survey(
        cfg,
        source,
        roadmap=roadmap,
        skip=skip,
        deep=False,
        stages=(),
        numbers=numbers,
        count_all_roadmaps=count_all_roadmaps,
    )
    if result.github_failed:
        return result
    result.dependency_wait = None
    if result.source_actor is None:
        result.errors.append("Forgejo discovery is public; native work requires an authenticated Forgejo account")
        return result
    if deep:
        from .survey_facts import in_scope

        result.open_prs = (
            [pr for pr in result.open_prs if pr.number in numbers]
            if numbers
            else [pr for pr in result.open_prs if in_scope(pr.labels, pr.target_focuses, pr.head_ref, roadmap, skip)]
        )
    pending = set()
    for path in (cfg.state / "review-work").glob("*/publication.json"):
        value = json.loads(path.read_text())
        if value.get("state") == "prepared" and value.get("source") == source.identity.record():
            pending.add((value["pr"], value["head"]))
    for path in (cfg.state / "review-work").glob("*/review-context.json"):
        if not path.with_name("publication.json").exists() and path.with_name("plan.json").is_file():
            value = json.loads(path.read_text())
            if value.get("source") == source.identity.record() and not value.get("complete"):
                pending.add((value["pr"], value["head"]))
    rs.observe(result.open_prs)
    for pr in result.open_prs:
        labels = {label.lower() for label in pr.labels}
        if pr.is_draft or labels & {"hold", "wip", "human", "on-hold"}:
            continue
        if (pr.number, pr.head_oid) in pending:
            continue
        meta = rs.gh_meta(pr.number)
        if meta.provenance not in {"fresh", "missing", "assumed"}:
            result.errors.append(f"Forgejo PR #{pr.number} review history unavailable")
            result.discovery_gaps += 1
            result.discovery_gap_prs.append(pr.number)
            continue
        owns = result.owns(pr)
        repair_allowed = not labels & {"keep", "do-not-close"}
        if owns and repair_allowed and "rebase" in stages and pr.mergeable == "CONFLICTING":
            key = counters.rebase_budget_key(pr.number)
            candidate = Candidate(
                pr.number,
                pr.head_oid,
                "conflicting",
                attempts=counters.read(key),
                budget=MAX_REBASE_ATTEMPTS,
                rebase_budget_key=key,
            )
            bucket = (
                result.rebaseable.suppressed if candidate.attempts >= candidate.budget else result.rebaseable.actionable
            )
            bucket.append(candidate)
            continue
        states = meta.data.get("states") or {}
        has_findings = (
            any(value in {"blocking_request", "blocking_block"} for value in states.values())
            if states
            else any(run.get("verdict") in {"request_changes", "block"} for run in meta.data.get("runs", []))
        )
        blocking = has_findings and rs.ledger_blocking(pr.number, pr.head_oid)
        reply = rs.newest_contest_reply(pr.number) if blocking else None
        through = meta.data.get("replies_through", 0)
        contest = reply if isinstance(reply, dict) and reply["id"] > through else None
        if owns and repair_allowed and "fix" in stages and blocking and not contest:
            candidate = Candidate(
                pr.number,
                pr.head_oid,
                "current source review requests changes",
                attempts=counters.read(f"fix-{pr.number}-{pr.head_oid[:12]}"),
                budget=MAX_FIX_ATTEMPTS,
                final_resolution=completed_attempts(counters, f"fix-{pr.number}-{pr.head_oid[:12]}")
                >= MAX_FIX_ATTEMPTS - 1,
            )
            bucket = (
                result.needs_fix.suppressed if candidate.attempts >= candidate.budget else result.needs_fix.actionable
            )
            bucket.append(candidate)
        if (
            "review" in stages
            and (contest or rs.ledger_clean_head(pr.number) != pr.head_oid)
            and not _unchanged_review_recent(counters, pr.number, pr.head_oid)
        ):
            # Review may run while mixed queue CI is being installed. The engine
            # receives unknown CI, and source statuses confer no build authority.
            candidate = Candidate(
                pr.number,
                pr.head_oid,
                "source head awaits review; execution CI not yet observed",
                attempts=counters.read(f"review-err-{pr.number}"),
                budget=MAX_REVIEW_ERRORS,
                contest=contest["rubric"] if contest else "",
                contest_reply_id=contest["id"] if contest else 0,
            )
            if contest and (
                counters.read(f"review-contest-{pr.number}") >= MAX_REVIEW_CONTESTS
                or counters.read(f"review-contest-{pr.number}-{contest['rubric']}") >= MAX_REVIEW_CONTESTS_PER_RUBRIC
            ):
                candidate.attempts = candidate.budget
            bucket = (
                result.reviewable.suppressed if candidate.attempts >= candidate.budget else result.reviewable.actionable
            )
            bucket.append(candidate)
    for stage in ("rebase", "review", "fix", "fix-ci", "bump", "lint-repair"):
        kind = result.kind(stage)
        for candidate in kind.actionable + kind.suppressed:
            candidate.source_repository = source.repository
            candidate.source_repository_id = source.identity.native_id
    result.review_stuck = [
        candidate.pr
        for candidate in result.reviewable.suppressed
        if counters.read(f"review-err-{candidate.pr}") >= MAX_REVIEW_ERRORS
    ]
    result.n_reviewable = len(result.reviewable.actionable)
    return result

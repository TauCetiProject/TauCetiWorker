"""Direct source inventory and qualified ownership in the ordinary survey consumer.

Native commit statuses are observations, not trusted execution evidence. The
inventory remains read-only; worker_survey adds native review and repair selection.
"""

from __future__ import annotations

import random
from dataclasses import asdict, dataclass

from tauceti_tools.forge import Incomplete, RepositoryRef

from .constants import MERGEABILITY_CHECKS, REFRESH_FAILURE_LIMIT, REFRESH_LOCK_WAIT_S
from .source_identity import SourceActor, maintains_author
from .survey import PRInfo, Survey, bucket_status_labels, roadmap_open_count


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

    def authored(self, pr: PRInfo) -> bool:
        """PRs this account opened itself; authoring pressure counts only these."""
        return bool(
            isinstance(pr, SourcePRInfo)
            and pr.source_repository.key == self.source_repository.key
            and self.source_actor is not None
            and pr.source_author == self.source_actor
        )

    def owns(self, pr: PRInfo) -> bool:
        """PRs this worker maintains: its own, plus adopted Forgejo authors' same-repository PRs."""
        if self.authored(pr):
            return True
        return bool(
            isinstance(pr, SourcePRInfo)
            and pr.source_repository.key == self.source_repository.key
            and pr.source_head_repository is not None
            and pr.source_head_repository.key == self.source_repository.key
            and pr.source_author is not None
            and maintains_author(
                self.source_actor, pr.source_author.principal.forge, int(pr.source_author.principal.native_id)
            )
        )

    def owns_author(self, author: dict | None) -> bool:
        if self.source_actor is None:
            return False
        try:
            actor = SourceActor.from_projection(self.source_repository.forge, author)
        except Incomplete:
            return False
        return maintains_author(self.source_actor, actor.principal.forge, int(actor.principal.native_id))


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


def discovery_key(source):
    """The shared listing's cache key; refresh_pr_state corrects one row under it."""
    from .source_identity import discovery_scope

    return [*discovery_scope(source), list(source.inventory_fields), "inventory"]


def discovery_inventory(source, *, numbers=None):
    """Share advisory discovery, leaving explicit targets and fresh reads direct.

    A shared list can lag writes for 60 seconds after its completed fetch. It
    never supplies a claim, review verdict or permission to publish; dispatch
    still validates the selected PR through its source. Selected-state corrections
    invalidate the qualified discovery generation. A source with `inventory`
    refreshes the last stored listing, whatever its age, instead of listing again.
    """
    if getattr(source, "shared_inventory", False) is not True or numbers:
        return source.open_prs()
    from . import github_transport as transport
    from .source_identity import discovery_scope

    key, identity = discovery_key(source), source.cache_namespace
    inventory = getattr(type(source), "inventory", None)

    def fetch():
        if inventory is not None:
            return inventory(source, transport.stored_shared(key, identity=identity))
        rows = source.open_prs(fresh=True)
        return None if rows is None else {"rows": rows}

    value = transport.shared_read(
        key,
        fetch,
        identity=identity,
        ttl=60,
        lock_timeout=300,
        generation_scope=discovery_scope(source),
        coalesce_failures=True,
        # A full listing outlasts many writes; once stored, a refresh brings it up to date.
        keep_base=True,
    )
    return None if value is None else value["rows"]


def inventory_survey(cfg, source, *, roadmap, skip, deep, stages, numbers, count_all_roadmaps, umbrella=None):
    repository = source.repository
    if not isinstance(repository, RepositoryRef) or repository.forge.kind != "forgejo":
        raise ValueError("source survey requires an explicit Forgejo repository")
    result = SourceSurvey(worker_id=cfg.wid, roadmap_only=roadmap, roadmap_skip=skip, source_repository=repository)
    result._umbrella = umbrella

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
    raw = discovery_inventory(source, numbers=numbers)
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
            [p for p in prs if not p.is_draft and result.authored(p)], "auto", skip
        )
    if deep:
        prs = [p for p in prs if (p.number in numbers if numbers else result.covers(p))]
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
    result._mine_open_prs = [p for p in nondraft if result.authored(p)]
    result.rescope_roadmap()
    result.status_labels, result.n_status_unlabeled = bucket_status_labels(
        nondraft, result.me_login, owns=result.authored
    )
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


_PAUSED = {"keep", "hold", "wip", "human", "on-hold", "do-not-close"}


def _labels(pr):
    return {label.lower() for label in pr.labels}


def _nondraft(result):
    return [p for p in result.open_prs if not p.is_draft and not (p.automatic_bump and _labels(p) & _PAUSED)]


def _tended(result):
    # classify_survey's `tended` for a native source: no bot-on-canonical repair, no queue-owned PRs.
    return [p for p in _nondraft(result) if result.owns(p) and not _labels(p) & (_PAUSED | {"bors-staging"})]


def index_order(result, counters, stages, pending):
    """The PRs whose review state could make them fix or review work, with the stages they could be.

    The filters are classify_survey's own (draft, pause labels, ownership, pending publication, a
    recent unchanged review), so no PR it could not select is read. Automatic Mathlib bumps first,
    then owned PRs, then everyone else's.
    """
    from .survey import _unchanged_review_recent

    reviewable = (
        {
            p.number
            for p in _nondraft(result)
            if not _labels(p) & {"hold", "wip", "human", "on-hold"}
            and (p.number, p.head_oid) not in pending
            and not _unchanged_review_recent(counters, p.number, p.head_oid)
        }
        if "review" in stages
        else set()
    )
    owned = {p.number for p in _tended(result)} if "fix" in stages else set()
    entries = [
        (p, frozenset(({"fix"} if p.number in owned else set()) | ({"review"} if p.number in reviewable else set())))
        for p in _nondraft(result)
        if p.number in owned | reviewable
    ]
    entries.sort(key=lambda entry: (not entry[0].automatic_bump, entry[0].number not in owned))
    return entries


def _wants_thread(rs, p, stages):
    """classify_survey reads the contest thread of a cleanly reviewed head, and of an owned head
    with blocking findings. Call only when the PR's scoreboard is current, so no remote read."""
    return ("review" in stages and rs.ledger_clean_head(p.number) == p.head_oid) or (
        "fix" in stages and rs.ledger_blocking(p.number, p.head_oid)
    )


def _needs_read(rs, p, stages):
    if not rs.current(p.number):
        return True
    return _wants_thread(rs, p, stages) and not rs.current(p.number, contest=True)


def refresh_index(rs, entries):
    """Bring the host's review-state index up to date for `entries`; return (reads, unrefreshed).

    Every PR whose current (updatedAt, head) has no observation is read, so each change to a PR
    costs the host one read, whichever worker notices it first. One worker refreshes at a time;
    the others wait, recheck, and find the PRs it read already current. A run of failed reads
    stops the refresh (the source is unavailable) and the next survey continues it.
    """
    stale = [(p, stages) for p, stages in entries if _needs_read(rs, p, stages)]
    if not stale:
        return 0, 0
    # Classification reads the whole index, so refresh order matters only when a refresh ends
    # early. Random order keeps a few PRs that persistently fail to read from ending every refresh.
    random.shuffle(stale)
    reads = failures = 0
    with rs.refresh_lock(REFRESH_LOCK_WAIT_S) as held:
        if not held:
            return 0, len(stale)
        for index, (p, stages) in enumerate(stale):
            failed = False
            if not rs.current(p.number):
                reads += 1
                failed = rs.gh_meta(p.number).provenance in ("stale", "fetch_failed")
            if not failed and _wants_thread(rs, p, stages) and not rs.current(p.number, contest=True):
                reads += 1
                rs.newest_contest_reply(p.number)
                failed = not rs.current(p.number, contest=True)  # a failed thread read stores nothing
            failures = failures + 1 if failed else 0
            if failures >= REFRESH_FAILURE_LIMIT:
                return reads, len(stale) - index - 1
    return reads, 0


def worker_survey(
    cfg, source, rs, counters, *, roadmap, skip, deep, stages, numbers, count_all_roadmaps, umbrella=None
):
    """Schedule review and owned-branch repair without asserting native CI authority."""
    from .survey import classify_survey

    result = inventory_survey(
        cfg,
        source,
        roadmap=roadmap,
        skip=skip,
        deep=False,
        stages=(),
        numbers=numbers,
        count_all_roadmaps=count_all_roadmaps,
        umbrella=umbrella,
    )
    if result.github_failed:
        return result
    result.dependency_wait = None
    if result.source_actor is None:
        result.errors.append("Forgejo discovery is public; native work requires an authenticated Forgejo account")
        return result
    if deep:
        result.open_prs = (
            [pr for pr in result.open_prs if pr.number in numbers]
            if numbers
            else [pr for pr in result.open_prs if result.covers(pr)]
        )
    rs.observe(result.open_prs)
    from contextlib import nullcontext
    from dataclasses import replace

    from .github_transport import read_context
    from .review_runtime import pending_reviews

    resolver = getattr(type(source), "resolve_mergeability", None)
    resolve = deep and "rebase" in stages and resolver is not None
    pending = pending_reviews(cfg) if deep else set()
    with read_context(advisory=True):
        # Native false conflates conflicts with checking/error states. Each check is a Git fetch,
        # so a survey resolves a few ambiguous owned PRs, in random order, and stops at a conflict.
        resolved = {}
        ambiguous = [pr for pr in (_tended(result) if resolve else ()) if pr.mergeable == "UNKNOWN"]
        random.shuffle(ambiguous)
        for pr in ambiguous if numbers else ambiguous[:MERGEABILITY_CHECKS]:
            resolved[pr.number] = resolver(source, cfg.state, pr.number, pr.head_oid)
            if resolved[pr.number] == "CONFLICTING" and not numbers:
                break
        result.open_prs = [
            replace(pr, mergeable=resolved[pr.number]) if pr.number in resolved else pr for pr in result.open_prs
        ]
        if deep and callable(getattr(type(rs), "cache_only", None)):
            result.detail_reads, result.detail_deferred = refresh_index(
                rs, index_order(result, counters, stages, pending)
            )
            classify_context = rs.cache_only()
        else:
            classify_context = nullcontext()
        with classify_context:
            classify_survey(
                cfg,
                source,
                rs,
                counters,
                result,
                enabled=set(stages) & {"review", "fix", "rebase"},
                deep=deep,
                review_without_ci=True,
                supports_external_repair=False,
                pending=pending,
            )
    for stage in ("rebase", "review", "fix", "fix-ci", "bump", "lint-repair"):
        kind = result.kind(stage)
        for candidate in kind.actionable + kind.suppressed:
            candidate.source_repository = source.repository
            candidate.source_repository_id = source.identity.native_id
    return result

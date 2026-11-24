"""Roadmap authoring backpressure counts the account's open PRs on every forge the project uses.

The limit is the account's: a worker that surveys one forge still authors against the open
non-draft roadmap PRs the account has on the other. A worker reads the other forge's count
cheaply (the shared GitHub state's author counts, or the host's shared Forgejo listing), counted
across all roadmap areas outside --roadmap-skip, so in a focused round it is at least as strict as
the round's own scope. An unavailable count is unknown, never zero: authoring waits for it.
"""

from __future__ import annotations

import os
import time

from tauceti_tools.forge import ForgeError

from .config import Die, log
from .constants import TAUCETI


def project_forges(cfg) -> tuple[str, ...]:
    """The forges whose open PRs count against the account's authoring limit.

    Forgejo counts when the project uses it and this worker holds a Forgejo credential, which is
    the account's identity there. A worker without one (a contributor's GitHub-only worker) has no
    Forgejo account whose PRs could be counted; its report names only GitHub. A credential that
    exists but cannot be read leaves the count unknown, which blocks authoring."""
    canonical = TAUCETI.lower() == "taucetiproject/tauceti"
    if not (os.environ.get("TAUCETI_FORGEJO_ORIGIN") or canonical):
        return ("github",)
    path = os.environ.get("TAUCETI_FORGEJO_TOKEN_FILE")
    if path is None and canonical and getattr(cfg, "home", None) is not None:
        path = str(cfg.home / ".config/tauceti-forgejo/worker-token")
    if not path:
        return ("github",)
    try:
        os.lstat(path)
    except FileNotFoundError:
        return ("github",)
    except OSError:
        pass  # Unreadable is not absent: count Forgejo, whose read then fails closed.
    return ("github", "forgejo")


NO_PR = 2**31 - 1  # the service accepts only positive PR numbers


def github_open(gh, skip) -> int | None:
    """The account's open non-draft roadmap PRs on GitHub, or None when unknown."""
    from .github import GitHubError, me
    from .github_state import StateQueryError, read_survey
    from .github_state import enabled as github_state_enabled
    from .github_transport import read_context
    from .survey import PRInfo, roadmap_open_count

    try:
        with read_context(retry=False):
            login = me()
            if github_state_enabled():
                # No PR has this number, so the service answers with its inventory-wide counts
                # alone; one shared response serves every worker on the host.
                available, rows = read_survey(gh.repo, "any", skip, numbers=(NO_PR,), stages=())
                if available and not rows.cached and rows.author_counts is not None:
                    return rows.author_counts.get(login, 0)
            prs = [PRInfo.from_json(row) for row in gh.open_prs()]
    except (Die, GitHubError, StateQueryError, ForgeError, OSError, RuntimeError, ValueError, KeyError, TypeError):
        return None
    if not login:
        return None
    return roadmap_open_count([p for p in prs if not p.is_draft and p.author == login], "auto", list(skip))


def forgejo_open(cfg, skip) -> int | None:
    """The account's open non-draft roadmap PRs on Forgejo, or None when unknown."""
    from .source_survey import inventory_survey
    from .worker_sources import native_source, source_config

    try:
        source = native_source(cfg)
        result = inventory_survey(
            source_config(cfg, source),
            source,
            roadmap="auto",
            skip=list(skip),
            deep=False,
            stages=(),
            numbers=(),
            count_all_roadmaps=True,
        )
    except (Die, ForgeError, OSError, RuntimeError, ValueError, KeyError, TypeError):
        return None
    # An anonymous listing cannot tell which PRs are the account's.
    if result.github_failed or result.source_actor is None:
        return None
    return result.own_open


def read_other(name, worker, skip) -> int | None:
    return github_open(worker.gh, skip) if name == "github" else forgejo_open(worker.cfg, skip)


def count_account(worker, surveyed, *, unknown=()) -> dict[str, int | None]:
    """Apply account-wide counts to `surveyed` (forge name -> its healthy survey) and return the
    per-forge counts: a surveyed forge counts from its survey, a forge in `unknown` (its survey
    failed this round) is unknown, and any other project forge is read."""
    if not surveyed:
        return {}
    first = next(iter(surveyed.values()))
    # An explicitly selected area wins over an overlapping skip, as in the round's own count.
    skip = [area for area in first.roadmap_skip if area != first.roadmap_only]
    for result in surveyed.values():
        result.count_account({})  # its own forge's count, from its own survey
    counts: dict[str, int | None] = {name: result.own_open for name, result in surveyed.items()}
    counts.update({name: None for name in unknown if name not in counts})
    for name in project_forges(getattr(worker, "cfg", None)):
        if name not in counts:
            counts[name] = read_other(name, worker, skip)
            if counts[name] is None:
                log(f"{name}: account open-PR count unavailable; roadmap authoring waits for it")
    for name, result in surveyed.items():
        result.count_account({other: count for other, count in counts.items() if other != name})
    return counts


def report(counts, result) -> None:
    """Publish the authoring backpressure this round enforces, for supervisors such as Fleet:
    the account's count on every project forge, and whether every count was known."""
    from .runtime_status import report_runtime

    complete = None not in counts.values()
    count = sum(value for value in counts.values() if value is not None)
    # A Fleet-focused round counts every roadmap while it works in one area: report the scope the
    # count covers, not the area selected for work.
    counted_all = getattr(result, "_automatic_open_count", None) is not None
    report_runtime(
        authoring=dict(
            observed_at=time.time(),
            sources={name: value for name, value in counts.items() if value is not None},
            forges=sorted(counts),
            complete=complete,
            count=count,
            limit=result.max_open_prs,
            blocked=not complete or count >= result.max_open_prs,
            scope="any" if counted_all else result.roadmap_only or "any",
            skip=",".join(sorted(set(filter(None, result.roadmap_skip)))),
        )
    )

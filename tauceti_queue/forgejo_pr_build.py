"""PR builds of Forgejo PR heads (TauCeti decision 215, option 1).

On GitHub, opening or pushing a PR fires `pull_request_target`, which runs TauCeti's `pr-build` and
posts `build`. Nothing fires for a Forgejo PR, so the queue starts the same build itself and copies
the result back:

- **Heads.** Every Forgejo PR the native poller lists (`NativePoller._observe_listed`) is recorded
  here at its current head, once per head (`observe`). A head is built unless the PR is a draft,
  targets another branch than the canonical one, or carries `self-tested` without `full-lint` or
  `lint-repair` (the labels for which GitHub's pr-build skips the build too). The label is read
  again at each listing, so adding or removing it before the build starts is honoured. A head
  superseded by a push, or a PR closed, before its build starts is never built.
- **Build.** The head is retained on GitHub under the queue's input ref (`Controller.input_ref`,
  the ref admission already pushes) and, once that push is done, dispatched as a `main-integration`
  build of the head merged onto the current canonical commit, with `source` `forgejo` (so the
  workflow never reads the same-numbered GitHub PR) and `priority` `pr` (queued on the Fleet PR
  class like any PR build; TauCetiTools `docs/main-integration.md`). At most
  `forgejo_pr_build_max_in_flight` heads are being pushed or built at once, oldest first.
- **Result.** Every outstanding head's `main-integration` status is read in one GraphQL query per
  pass (every `forgejo_pr_build_interval_seconds`); there is no REST polling. Only a verdict posted
  after the dispatch by an account in `forgejo_pr_build_status_creators` answers (`answers`). Its state, description and run URL are posted on the Forgejo head as the `build` status,
  by the queue's Forgejo publisher, through the outbox (`perform`). A `pending` `build` is posted
  at dispatch. An `error` (no verdict: a conflict with main, a guard or infrastructure) is asked
  once more before it is posted; a deferral (the picker failed) or a build without a result within
  `forgejo_pr_build_timeout_seconds` is asked again, up to `MAX_ATTEMPTS` dispatches in all.

No Forgejo token is sent to Actions: the build reads the head from GitHub, and only the queue
writes to Forgejo. Workers and admission trust a Forgejo `build` only from the publisher. Off until
`forgejo_pr_build_enabled`, and inert unless Forgejo is in `admit` mode and publication is enabled.
"""

from __future__ import annotations

import re
import time
from typing import Any

from . import chain
from .store import dumps, loads

WANTED, SKIPPED, RETAINING, PENDING = "wanted", "skipped", "retaining", "pending"
SUCCESS, FAILURE, ERROR, SUPERSEDED = "success", "failure", "error", "superseded"
FINAL = (SUCCESS, FAILURE, ERROR, SUPERSEDED)
IN_FLIGHT = (RETAINING, PENDING)
CONTEXT = "build"
SELF_TESTED, FULL_LINT, LINT_REPAIR = "self-tested", "full-lint", "lint-repair"
# Dispatches of one head, counting the ones that gave no verdict; an `error` verdict counts too.
MAX_ATTEMPTS = 3
# A head whose retained ref has not reached GitHub after this many outbox attempts is reported.
MAX_RETAIN_ATTEMPTS = 6
# Heads fetched from Forgejo in one pass, and the wall time those fetches may take.
FETCHES_PER_PASS = 8
FETCH_SECONDS = 60
# `main_integration_statuses` reads 50 heads a query: more in flight would need a second query.
MAX_IN_FLIGHT = 50
PASS_AT = "forgejo_pr_build_pass_at"


def enabled(ctl) -> bool:
    mixed = getattr(ctl, "mixed", None)
    execution = ctl.execution
    return bool(
        ctl.setting("forgejo_pr_build_enabled")
        and ctl.setting("publication_enabled")
        and mixed is not None
        and mixed.mode == "admit"
        and callable(getattr(execution, "dispatch_main_integration", None))
        and callable(getattr(execution, "main_integration_statuses", None))
    )


def _source_key(ctl) -> str:
    return ctl.mixed.profile.forgejo.repository.key


def skip_reason(ctl, pull) -> str:
    """Why this listed head gets no PR build, or "" when it gets one. A listing item carries its
    labels in its `admission` details; a full PR read carries them itself."""
    labels = getattr(pull, "labels", None)
    if labels is None:
        labels = (getattr(pull, "admission", None) or {}).get("labels", ())
    labels = set(labels or ())
    if getattr(pull, "draft", False):
        return "draft"
    if getattr(pull, "base_ref", ctl.canonical_branch) != ctl.canonical_branch:
        return "base"
    if SELF_TESTED in labels and not labels & {FULL_LINT, LINT_REPAIR}:
        return SELF_TESTED
    return ""


# --- heads ---------------------------------------------------------------------------------------


def observe(ctl, pull) -> None:
    """Record a listed Forgejo PR at its current head (any thread; the store serializes)."""
    if not enabled(ctl) or getattr(pull, "state", "open") != "open":
        return
    head = getattr(pull, "head_sha", "")
    if not isinstance(head, str) or len(head) != 40:
        return
    source, number, now = _source_key(ctl), int(pull.number), ctl.clock()
    reason = skip_reason(ctl, pull)
    with ctl.store.transaction():
        for row in ctl.store.rows("pr_builds", {"source": source, "number": number}):
            if row["head_sha"] != head and row["state"] in (WANTED, SKIPPED, RETAINING):
                ctl.store.update("pr_builds", {"id": row["id"]}, state=SUPERSEDED, settled_at=now)
        row = ctl.store.row("pr_builds", {"source": source, "number": number, "head_sha": head})
        if row is None:
            ctl.store.insert(
                "pr_builds",
                source=source,
                number=number,
                head_sha=head,
                state=SKIPPED if reason else WANTED,
                reason=reason,
                observed_at=now,
            )
        elif row["state"] in (SKIPPED, SUPERSEDED) and not reason:
            # A label withdrawn, or a head listed again (a push undone, a PR reopened): never built,
            # since a head is superseded only before its dispatch.
            ctl.store.update("pr_builds", {"id": row["id"]}, state=WANTED, reason="", retry_at=0, settled_at=None)
        elif row["state"] in (WANTED, RETAINING, SUPERSEDED) and reason:
            # Before its dispatch, a head that gains `self-tested` (or becomes a draft) is not built.
            ctl.store.update("pr_builds", {"id": row["id"]}, state=SKIPPED, reason=reason, settled_at=None)


def building_heads(ctl) -> set[str]:
    """The Forgejo heads whose PR build is pending: the backfill leaves them to it."""
    if getattr(ctl, "mixed", None) is None:
        return set()
    return {row["head_sha"] for row in _rows(ctl, PENDING)}


def _current(ctl, row: dict[str, Any]) -> bool:
    """The PR is still open at this head, as the native poller last saw it (no API call)."""
    work = ctl.store.row("admission_work", {"repository": row["source"], "number": row["number"]})
    return work is not None and work["head_sha"] == row["head_sha"] and work["disposition"] not in ("closed", "draft")


# --- one pass ------------------------------------------------------------------------------------


def run(ctl, now: float) -> None:
    """One pass, from the controller's tick with its lock held."""
    if not enabled(ctl):
        return
    if now - (ctl._state(PASS_AT) or 0) < ctl.setting("forgejo_pr_build_interval_seconds"):
        return
    ctl._set_state(PASS_AT, now)
    _observe(ctl, now)
    _dispatch_retained(ctl, now)
    _start(ctl, now)


def _rows(ctl, *states: str) -> list[dict[str, Any]]:
    source = _source_key(ctl)
    return [row for state in states for row in ctl.store.rows("pr_builds", {"source": source, "state": state})]


def _start(ctl, now: float) -> None:
    """Retain the oldest wanted heads on GitHub, within the in-flight budget.

    A head the clone lacks is fetched from Forgejo with the controller lock let go, at most
    `FETCHES_PER_PASS` heads and `FETCH_SECONDS` a pass; one that cannot be fetched waits
    `forgejo_pr_build_retry_seconds` and the others go on. Every transition is conditional on the
    row being unchanged, since the poller thread records observations meanwhile."""
    limit = min(ctl.setting("forgejo_pr_build_max_in_flight"), MAX_IN_FLIGHT)
    budget = limit - len(_rows(ctl, *IN_FLIGHT))
    if budget <= 0:
        return
    wanted = sorted(
        (row for row in _rows(ctl, WANTED) if row["retry_at"] <= now),
        key=lambda row: (row["observed_at"], row["id"]),
    )
    fetches, deadline = 0, time.monotonic() + FETCH_SECONDS
    retry = now + ctl.setting("forgejo_pr_build_retry_seconds")
    for row in wanted:
        if budget <= 0:
            break
        if not _current(ctl, row):
            ctl.store.update("pr_builds", {"id": row["id"], "state": WANTED}, state=SUPERSEDED, settled_at=now)
            continue
        number = ctl.mixed.number(row["number"])
        if not chain.object_exists(ctl.clone, row["head_sha"]):
            if fetches >= FETCHES_PER_PASS or time.monotonic() > deadline:
                break
            fetches += 1
            try:
                with ctl._lock.unlocked():
                    fetched = ctl._fetch_head(number, row["head_sha"])
            except Exception as error:  # noqa: BLE001 - this head waits; the others go on
                ctl.log.warning("fetch of Forgejo head %s failed: %s", row["head_sha"], error)
                fetched = False
            if not fetched:
                ctl._reasons.append(f"Forgejo PR build of {row['number']}: head {row['head_sha'][:12]} unavailable")
                ctl.store.update("pr_builds", {"id": row["id"], "state": WANTED}, retry_at=retry)
                continue
        ref = ctl.input_ref(number, row["head_sha"])
        with ctl.store.transaction():
            if not ctl.store.update("pr_builds", {"id": row["id"], "state": WANTED}, state=RETAINING, ref=ref):
                continue  # observed meanwhile (skipped or superseded)
            ctl._retain(ref, row["head_sha"])
        budget -= 1


def _dispatch_retained(ctl, now: float) -> None:
    main = ctl._canonical
    for row in _rows(ctl, RETAINING):
        entry = ctl.store.row("outbox", {"key": f"retain:{row['ref']}:{row['head_sha']}"})
        if entry is None or entry["state"] != "done":
            if entry is not None and entry["attempts"] >= MAX_RETAIN_ATTEMPTS:
                _final(ctl, row, ERROR, "the head could not be pushed to GitHub for its PR build", "", now)
            continue
        if not main:
            return
        if not _current(ctl, row):
            ctl.store.update("pr_builds", {"id": row["id"], "state": RETAINING}, state=SUPERSEDED, settled_at=now)
            continue
        try:
            with ctl._lock.unlocked():
                ctl.execution.dispatch_main_integration(
                    ctl.repository,
                    pr=int(row["number"]),
                    head_sha=row["head_sha"],
                    main_sha=main,
                    source="forgejo",
                    priority="pr",
                )
        except Exception as error:  # noqa: BLE001 - the next pass tries again
            ctl._reasons.append(f"Forgejo PR build of {row['number']} not dispatched: {error}")
            return
        fresh = ctl.store.row("pr_builds", {"id": row["id"]})
        if fresh is None or fresh["state"] != RETAINING:
            continue  # skipped meanwhile: the build runs, and its result is not posted
        with ctl.store.transaction():
            if not ctl.store.update(
                "pr_builds",
                {"id": row["id"], "state": RETAINING},
                state=PENDING,
                main_sha=main,
                dispatched_at=now,
                attempts=fresh["attempts"] + 1,
            ):
                continue
            _post(ctl, fresh, "pending", f"B={main[:12]} queued: the PR build runs on GitHub Actions", "")
            ctl._count("forgejo_pr_build_dispatched")
            ctl.store.journal(
                now, "forgejo-pr-build-dispatched", None, number=row["number"], head=row["head_sha"], main=main
            )


# --- results -------------------------------------------------------------------------------------


def _observe(ctl, now: float) -> None:
    rows = _rows(ctl, PENDING)
    if not rows:
        return
    try:
        with ctl._lock.unlocked():
            statuses = ctl.execution.main_integration_statuses(ctl.repository, [row["head_sha"] for row in rows])
    except Exception as error:  # noqa: BLE001 - the next pass reads them again
        ctl._reasons.append(f"Forgejo PR build statuses unavailable: {error}")
        return
    trusted = set(ctl.setting("forgejo_pr_build_status_creators"))
    timeout = ctl.setting("forgejo_pr_build_timeout_seconds")
    for row in rows:
        fresh = ctl.store.row("pr_builds", {"id": row["id"]})
        if fresh is None or fresh["state"] != PENDING:
            continue
        status = statuses.get(fresh["head_sha"])
        if answers(status, fresh, trusted):
            _settle(ctl, fresh, status, now)
        elif now - fresh["dispatched_at"] > timeout:
            _again(ctl, fresh, "no result within the timeout", now)


def answers(status, row: dict[str, Any], trusted: set[str]) -> bool:
    """The status answers this dispatch: a verdict of this head merged onto a main, posted after
    the dispatch by a trusted account, and not a run already counted for this head.

    `main-integration` is one context per commit, so the backfill's build of the same head on
    another main (`main_integration.py`) can be the latest status. Any trusted verdict on a main
    posted after the dispatch is a PR build of this head on a current main, so it answers too. A
    deferral answers only for the main asked for: the backfill's deferrals are its own."""
    if status is None or status.state not in (SUCCESS, FAILURE, ERROR) or not row["main_sha"]:
        return False
    if re.match(r"B=[0-9a-f]{12}", status.description) is None:
        return False
    deferral = status.state == ERROR and "deferred" in status.description
    if deferral:
        # The picker posts its deferral with the workflow's own token; a verdict only the CI App.
        if not status.description.startswith(f"B={row['main_sha'][:12]}"):
            return False
        if status.creator not in trusted and status.creator != "github-actions":
            return False
    elif status.creator not in trusted:
        return False
    if status.target_url and status.target_url in loads(row["runs"], []):
        return False
    posted = status.updated_at
    return posted is not None and posted >= row["dispatched_at"] - 120


def _settle(ctl, row: dict[str, Any], status, now: float) -> None:
    runs = loads(row["runs"], [])
    if status.target_url:
        runs = [*runs, status.target_url][-8:]
    ctl.store.update("pr_builds", {"id": row["id"]}, runs=dumps(runs))
    row = ctl.store.row("pr_builds", {"id": row["id"]})
    if status.state == ERROR and "deferred" in status.description:
        _again(ctl, row, status.description, now)
    elif status.state == ERROR and row["attempts"] < 2:
        _again(ctl, row, status.description, now)
    else:
        _final(ctl, row, status.state, status.description, status.target_url, now)


def _again(ctl, row: dict[str, Any], why: str, now: float) -> None:
    """Ask for this head again later, or report it once its attempts are spent."""
    if row["attempts"] >= MAX_ATTEMPTS:
        _final(ctl, row, ERROR, f"no verdict after {row['attempts']} builds: {why}", "", now)
        return
    with ctl.store.transaction():
        ctl.store.update(
            "pr_builds",
            {"id": row["id"]},
            state=WANTED,
            description=why[:200],
            retry_at=now + ctl.setting("forgejo_pr_build_retry_seconds"),
        )
        ctl.store.journal(
            now, "forgejo-pr-build-retry", None, number=row["number"], head=row["head_sha"], why=why[:200]
        )


def _final(ctl, row: dict[str, Any], state: str, description: str, url: str, now: float) -> None:
    with ctl.store.transaction():
        ctl.store.update(
            "pr_builds",
            {"id": row["id"]},
            state=state,
            description=description[:200],
            url=url,
            settled_at=now,
        )
        _post(ctl, row, state, description, url)
        ctl._count(f"forgejo_pr_build_{state}")
        ctl.store.journal(
            now,
            "forgejo-pr-build-result",
            None,
            number=row["number"],
            head=row["head_sha"],
            main=row["main_sha"],
            state=state,
            description=description[:200],
            url=url,
        )


# --- the Forgejo `build` status ------------------------------------------------------------------


def _post(ctl, row: dict[str, Any], state: str, description: str, url: str) -> None:
    """Enqueue the head's `build` status; only the newest intent for a head is ever posted."""
    intent = f"{row['id']}:{state}:{row['attempts']}:{ctl.clock()}"
    ctl.store.update("pr_builds", {"id": row["id"]}, posted=intent)
    ctl._enqueue(
        "source_status",
        f"forgejo-pr-build:{intent}",
        {
            "number": ctl.mixed.number(row["number"]),
            "build_id": row["id"],
            "intent": intent,
            "sha": row["head_sha"],
            "state": state,
            "description": description[:140],
            "target_url": url if url.startswith("https://github.com/") else "",
        },
    )


def perform(ctl, repo: str, payload: dict[str, Any]) -> str:
    """The outbox's `source_status` entry: post the `build` status unless a newer one superseded it."""
    row = ctl.store.row("pr_builds", {"id": payload["build_id"]})
    if row is None or row["posted"] != payload["intent"]:
        return "superseded"
    ctl.source.post_source_status(
        repo,
        payload["number"],
        payload["sha"],
        context=CONTEXT,
        state=payload["state"],
        description=payload["description"],
        target_url=payload["target_url"],
    )
    if payload["state"] in (SUCCESS, FAILURE, ERROR) and getattr(ctl, "admission", None) is not None:
        # A status does not move the PR's Forgejo update time: evaluate it again now, so admission
        # that waits for this build need not wait for the periodic recheck.
        ctl.admission.invalidate(payload["number"])
    return f"build {payload['state']}"

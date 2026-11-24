"""The main-integration backfill: test queued members against current main on idle PR CI (decision 175 B).

While a batch is open, its members were approved against bases that can be a day old, and most of
them (Forgejo PRs and self-tested GitHub PRs) were never built on a recent main. Once per
`main_integration_interval_seconds` this pass asks CI to build some of them merged onto the current
canonical commit, without pushing anything (TauCetiTools `docs/main-integration.md`):

- **Candidates** are the members of open, unsealed, non-emergency, non-lane batches whose input has
  no result younger than `main_integration_fresh_seconds` (GitHub sources only, for now). They are
  ordered by batch (the next to seal first), then by the cheap static check (`stale_check.py`:
  flagged first), then by the age of their base (oldest first). At most
  `main_integration_max_in_flight` builds are outstanding; CI itself runs one only on idle Fleet PR
  capacity, and otherwise reports it deferred, after which it is asked again.
- **Results** are the `main-integration` commit status on the accepted head, read for every
  outstanding head in one GraphQL query per pass. Only a status naming the main that was asked
  for and posted after the dispatch counts.
- **Green** is fresh evidence on the input. **Red** is asked once more (on the then-current main);
  a second red takes the member out of its batch while the batch is still unsealed and nobody holds
  it: the batch's chain is replayed without the member's commit, and the input is cancelled with
  reason `main-integration-failed`, its approval withdrawn and its head handed back to its worker or
  author by a head-bound recovery request naming the run. A member an agent has already edited, a
  sealed batch (including decision 174's verification snapshot) and a replay that conflicts are
  left alone; the result is only recorded. Errors (no verdict, a textual conflict) are recorded and
  never acted on.

The record is the input's `main_integration` JSON: `main`, `dispatched_at`, `state` (`pending`,
`deferred`, `success`, `failure`, `error`, `expired`), `description`, `url`, `observed_at`,
`failures` (red results so far), `expired` (timeouts so far), `retry_at`, `checked` (the static
check) and `acted` (what the take-out did). Everything is off until `main_integration_enabled`.
"""

from __future__ import annotations

import secrets
import time
from typing import Any

from . import chain, ports, stale_check
from .store import dumps, loads

PENDING, DEFERRED, SUCCESS, FAILURE, ERROR, EXPIRED = "pending", "deferred", "success", "failure", "error", "expired"
FINAL = (SUCCESS, FAILURE, ERROR)
REASON = "main-integration-failed"
# Red results before a take-out: one more run separates a source failure from a broken runner.
CONFIRMATIONS = 2
# Timeouts before an input is no longer asked about on the same main.
MAX_EXPIRED = 3


def record_of(row: dict[str, Any]) -> dict[str, Any]:
    value = loads(row.get("main_integration"), {})
    return value if isinstance(value, dict) else {}


def enabled(ctl) -> bool:
    execution = ctl.execution
    return bool(
        ctl.setting("main_integration_enabled")
        and not ctl.native_source
        and callable(getattr(execution, "dispatch_main_integration", None))
        and callable(getattr(execution, "main_integration_statuses", None))
    )


def run(ctl, now: float) -> None:
    """One pass of the backfill, called from the controller's tick with its lock held."""
    if not enabled(ctl):
        return
    last = ctl._state("main_integration_pass_at") or 0
    if now - last < ctl.setting("main_integration_interval_seconds"):
        return
    ctl._set_state("main_integration_pass_at", now)
    _observe(ctl, now)
    _take_out(ctl, now)
    _dispatch(ctl, _candidates(ctl), now)


# --- candidates ----------------------------------------------------------------------------------


def _eligible_batches(ctl) -> list[dict[str, Any]]:
    head = ctl._head()
    return [
        batch
        for batch in ctl._staging_batches()
        if not batch["sealed"]
        and (head is None or batch["id"] != head["id"])
        and not batch["lane"]
        and ctl._eviction(batch) is None
    ]


def _candidates(ctl) -> list[tuple[dict[str, Any], dict[str, Any], dict[str, Any]]]:
    """(batch, member, input) for every member the backfill may test, in batch order."""
    found = []
    for batch in _eligible_batches(ctl):
        for member in ctl._members(batch["id"]):
            if member["withdrawn"]:
                continue
            row = ctl.store.row("inputs", {"id": member["input_id"]})
            if (
                row is None
                or row["state"] != "inserted"
                or row["batch_id"] != batch["id"]
                or row["source_identity"]
                or row["head_sha"] != member["accepted_head"]
            ):
                continue
            found.append((batch, member, row))
    return found


def _save(ctl, row: dict[str, Any], record: dict[str, Any]) -> None:
    ctl.store.update("inputs", {"id": row["id"]}, main_integration=dumps(record))


# --- results -------------------------------------------------------------------------------------


def _observe(ctl, now: float) -> None:
    rows = [
        row
        for row in ctl.store.rows("inputs", {"repository": ctl.repository, "state": "inserted"})
        if record_of(row).get("state") == PENDING
    ]
    if not rows:
        return
    try:
        with ctl._lock.unlocked():
            statuses = ctl.execution.main_integration_statuses(ctl.repository, [row["head_sha"] for row in rows])
    except Exception as error:  # noqa: BLE001 - the next pass reads them again
        ctl._reasons.append(f"main-integration statuses unavailable: {error}")
        return
    timeout = ctl.setting("main_integration_timeout_seconds")
    for row in rows:
        fresh = ctl.store.row("inputs", {"id": row["id"]})
        if fresh is None:
            continue
        record = record_of(fresh)
        if record.get("state") != PENDING:
            continue
        status = statuses.get(fresh["head_sha"])
        if _answers(status, record):
            _settle(ctl, fresh, record, status, now)
        elif now - record.get("dispatched_at", now) > timeout:
            record.update(state=EXPIRED, observed_at=now, expired=record.get("expired", 0) + 1)
            with ctl.store.transaction():
                _save(ctl, fresh, record)
                ctl.store.journal(now, "main-integration-expired", fresh["batch_id"], number=fresh["number"])


def _answers(status: ports.Status | None, record: dict[str, Any]) -> bool:
    """The status is this request's: it names the main asked for and was posted after the dispatch."""
    if status is None or status.state not in (SUCCESS, FAILURE, ERROR):
        return False
    if not status.description.startswith(f"B={record.get('main', '')[:12]}"):
        return False
    posted = status.updated_at
    return posted is None or posted >= record.get("dispatched_at", 0) - 120


def _settle(ctl, row: dict[str, Any], record: dict[str, Any], status: ports.Status, now: float) -> None:
    state = status.state
    if state == ERROR and "deferred" in status.description:
        state = DEFERRED
        record["retry_at"] = now + ctl.setting("main_integration_deferred_retry_seconds")
    record.update(state=state, description=status.description[:200], url=status.target_url, observed_at=now)
    if state == FAILURE:
        record["failures"] = record.get("failures", 0) + 1
    elif state == SUCCESS:
        record["failures"] = 0
    with ctl.store.transaction():
        _save(ctl, row, record)
        if state != DEFERRED:
            ctl._count(f"main_integration_{state}")
            ctl.store.journal(
                now,
                "main-integration-result",
                row["batch_id"],
                number=row["number"],
                head=row["head_sha"],
                main=record.get("main"),
                state=state,
                description=status.description[:200],
                url=status.target_url,
            )


# --- red results: take the member out before seal ------------------------------------------------


def _take_out(ctl, now: float) -> None:
    if not ctl.setting("main_integration_take_out"):
        return
    for batch, member, row in _candidates(ctl):
        record = record_of(row)
        if record.get("state") != FAILURE or record.get("acted") or record.get("failures", 0) < CONFIRMATIONS:
            continue
        if member["provenance"] != "clean":
            # An agent already changed this member; its accepted head is no longer what lands.
            record["acted"] = "edited"
            with ctl.store.transaction():
                _save(ctl, row, record)
                ctl.store.journal(now, "main-integration-kept", batch["id"], number=row["number"], why="edited")
            continue
        if ctl._active_claims(batch["id"]):
            ctl._reasons.append(
                f"open batch {batch['id']}: main-integration take-out of {row['number']} waits for its claim"
            )
            continue
        _remove(ctl, batch, member, row, record, now)


def _remove(ctl, batch, member, row, record, now: float) -> None:
    """Replay the batch without `member`'s commit and send the PR back to its worker or author."""
    chain_id = f"main-integration-{batch['id']}-{secrets.token_hex(4)}"
    result = None
    error = None
    try:
        with ctl._lock.unlocked():
            commits = chain.chain_commits(ctl.clone, batch["base"], batch["tip"])
            index = next(i for i, commit in enumerate(commits) if commit.sha == member["commit_sha"])
            parent = commits[index].parents[0]
            if index + 1 < len(commits):
                result = chain.replay(ctl.clone, commits[index].sha, batch["tip"], parent, chain_id=chain_id)
            else:
                result = chain.ChainResult(chain.CLEAN, chain_id, chain.chain_ref(chain_id), parent, parent, [])
    except (StopIteration, chain.ChainError, IndexError) as failure:
        error = str(failure) or "member commit not in the chain"
    finally:
        chain.delete_chain_ref(ctl.clone, chain_id)
    current = ctl.store.row("batches", {"id": batch["id"]})
    if (
        current is None
        or current["version"] != batch["version"]
        or current["sealed"]
        or current["state"] != "open"
        or ctl._active_claims(batch["id"])
    ):
        return  # the batch moved, sealed or was claimed meanwhile: the next pass decides again
    if error is not None or result is None or result.status != chain.CLEAN:
        why = error or f"replay conflicts at {getattr(result.conflict, 'source_pr', '?')}"
        record["acted"] = "conflict"
        with ctl.store.transaction():
            _save(ctl, row, record)
            ctl.store.journal(now, "main-integration-kept", batch["id"], number=row["number"], why=why[:300])
        return
    shas = {record_.source_pr: record_.sha for record_ in result.commits}
    version = batch["version"] + 1
    with ctl.store.transaction():
        for other in ctl._members(batch["id"]):
            if other["id"] != member["id"] and other["source_pr"] in shas:
                ctl.store.update("members", {"id": other["id"]}, commit_sha=shas[other["source_pr"]])
        ctl.store.update("members", {"id": member["id"]}, removed=1, removed_reason=REASON)
        ctl.store.update("batches", {"id": batch["id"]}, version=version, tip=result.tip)
        ctl._record_tip(batch["id"], version, result.tip, now)
        ctl._retain(ctl.batch_ref(batch["id"], version), result.tip)
        for task in ctl.store.rows("tasks", {"batch_id": batch["id"], "state": "open"}):
            ctl.store.update("tasks", {"id": task["id"]}, state="superseded", resolved_at=now, resolution=REASON)
        ctl._set_pre_rebase(batch, "", now)
        ctl._discard_pre_rebase(REASON, now, ctl.store.row("batches", {"id": batch["id"]}) or batch)
        record["acted"] = "removed"
        _save(ctl, row, record)
        ctl.store.update("inputs", {"id": row["id"]}, state="cancelled", reason=REASON)
        if ctl.admission is not None:
            ctl.admission.withdraw(row["number"], row["head_sha"])
        ctl._count("main_integration_removed")
        ctl.store.journal(
            now,
            "main-integration-removed",
            batch["id"],
            number=row["number"],
            head=row["head_sha"],
            main=record.get("main"),
            url=record.get("url"),
            version=version,
            tip=result.tip,
        )
        if ctl.setting("publication_enabled"):
            _hand_back(ctl, member, row, record)
    ctl._refresh_snapshot()


def _hand_back(ctl, member, row, record) -> None:
    from .controller import COMMENT_MARKER

    head = row["head_sha"]
    body = (
        f"this head fails on current `{ctl.canonical_branch}` (`{record.get('main', '')[:12]}`): "
        f"{record.get('description', '')}. Run: {record.get('url') or 'unknown'}. The merge queue took it out of its "
        f"batch before the batch sealed, so it did not reach staging. Merge `{ctl.canonical_branch}` into the PR, "
        "fix what breaks and push; the new head returns to ordinary CI and review, and needs approving again."
    )
    marker = f"<!-- {COMMENT_MARKER} main-integration:{head} -->"
    ctl._recovery_request(f"recovery-main-integration:{row['number']}:{head}", row["number"], head, body, marker)
    label = ctl.setting("membership_label")
    if label:
        ctl._enqueue(
            "labels",
            f"main-integration-labels:{row['id']}",
            {"number": row["number"], "add": [], "remove": [label], "input_id": row["id"]},
        )


# --- dispatch ------------------------------------------------------------------------------------


def _due(record: dict[str, Any], now: float, fresh: float) -> bool:
    state = record.get("state")
    if not state:
        return True
    if state == PENDING:
        return False
    if state == DEFERRED:
        return now >= record.get("retry_at", 0)
    if state == EXPIRED:
        return record.get("expired", 0) < MAX_EXPIRED or now - record.get("dispatched_at", 0) > fresh
    if state == FAILURE:
        # A first red result is confirmed at once; a confirmed one waits for the take-out.
        return not record.get("acted") and record.get("failures", 0) < CONFIRMATIONS
    return now - record.get("dispatched_at", 0) > fresh


def _dispatch(ctl, candidates, now: float) -> None:
    in_flight = sum(
        1
        for row in ctl.store.rows("inputs", {"repository": ctl.repository, "state": "inserted"})
        if record_of(row).get("state") == PENDING
    )
    budget = ctl.setting("main_integration_max_in_flight") - in_flight
    main = ctl._canonical
    if budget <= 0 or not main:
        return
    fresh = ctl.setting("main_integration_fresh_seconds")
    due = [(batch, member, row) for batch, member, row in candidates if _due(record_of(row), now, fresh)]
    if not due:
        return
    _check(ctl, due, main, now)
    order = {batch["id"]: index for index, batch in enumerate(_eligible_batches(ctl))}

    def rank(item):
        batch, _member, row = item
        record = record_of(row)
        confirming = record.get("state") == FAILURE
        checked = record.get("checked")
        flag = 1 if checked is None else (0 if checked.get("flagged") else 2)
        return (
            not confirming,
            order.get(batch["id"], len(order)),
            flag,
            (checked or {}).get("base_time", row["approved_at"]),
        )

    due = [(batch, member, ctl.store.row("inputs", {"id": row["id"]})) for batch, member, row in due]
    for batch, _member, row in sorted((item for item in due if item[2] is not None), key=rank)[:budget]:
        try:
            with ctl._lock.unlocked():
                ctl.execution.dispatch_main_integration(
                    ctl.repository, pr=int(row["number"]), head_sha=row["head_sha"], main_sha=main
                )
        except Exception as error:  # noqa: BLE001 - the next pass tries again
            ctl._reasons.append(f"main-integration dispatch of {row['number']} not accepted: {error}")
            return
        fresh_row = ctl.store.row("inputs", {"id": row["id"]}) or row
        record = record_of(fresh_row)
        record.update(state=PENDING, main=main, dispatched_at=now, description="", url="")
        with ctl.store.transaction():
            _save(ctl, fresh_row, record)
            ctl._count("main_integration_dispatched")
            ctl.store.journal(
                now, "main-integration-dispatched", batch["id"], number=row["number"], head=row["head_sha"], main=main
            )


def _check(ctl, due, main: str, now: float) -> None:
    """Run the static check for up to `main_integration_checks_per_pass` due members that lack one."""
    limit = ctl.setting("main_integration_checks_per_pass")
    todo = [(batch, row) for batch, _member, row in due if record_of(row).get("checked") is None][:limit]
    if not todo:
        return
    checker = getattr(ctl, "_stale_check", None)
    if checker is None or checker.clone != str(ctl.clone):
        checker = ctl._stale_check = stale_check.StaleCheck(ctl.clone)
    results = {}
    deadline = time.monotonic() + 60
    with ctl._lock.unlocked():
        for _batch, row in todo:
            if time.monotonic() > deadline:
                break
            result = checker.check(row["head_sha"], main)
            if result.get("base"):
                try:
                    result["base_time"] = int(chain.git(ctl.clone, "log", "-1", "--format=%ct", result["base"]).strip())
                except (chain.ChainError, ValueError):
                    pass
            results[row["id"]] = result
    with ctl.store.transaction():
        for input_id, result in results.items():
            row = ctl.store.row("inputs", {"id": input_id})
            if row is None:
                continue
            record = record_of(row)
            record["checked"] = dict(result, main=main, at=now)
            _save(ctl, row, record)

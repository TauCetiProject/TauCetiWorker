"""Asking the merge gate again about a progress report it has left open.

The roadmap repository's gate considers a report on an event of its pull request, when its
workflow is dispatched and, where the repository gives it one, on a schedule that retries reports
held for the merge queue. With progress_shepherd on, each progress round
dispatches that workflow for a report of this account that is still open, a bounded number of
times, and then records the report for a person. The timings, the ledger and the permission the
dispatch needs are under "Open progress reports" in docs/reference.md.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from urllib.parse import urlencode

from . import github_transport as transport
from . import incidents
from .config import log, one_line, warn_red
from .constants import (
    PROGRESS_MERGE_WORKFLOW,
    PROGRESS_SHEPHERD_ASKS,
    PROGRESS_SHEPHERD_GRACE,
    PROGRESS_SHEPHERD_INTERVAL,
    PROGRESS_SHEPHERD_RECHECK,
    ROADMAP,
)
from .github import GitHub, GitHubError, _gh_rate_kind, _parse_iso8601, me, run
from .work_units import progress_reports

LEDGER = "progress/shepherd.json"
# The roadmap repository whose ledger keeps the name LEDGER; any other gets its own file.
DEFAULT_ROADMAP = "TauCetiProject/TauCetiRoadmap"
FIELDS = ["number", "headRefName", "headRefOid", "createdAt", "isDraft", "url"]
# The opening words of the comment the gate leaves when it refuses a report; its reason follows in
# a fenced block.
REFUSAL = "progress gate did not merge"


def ledger_path(cfg) -> Path:
    """The ledger of the configured roadmap repository: LEDGER for DEFAULT_ROADMAP, and
    `shepherd.<digest>.json` beside it for any other, the digest as in incidents.subject."""
    key = incidents.repository_ref(ROADMAP).key
    if key == incidents.repository_ref(DEFAULT_ROADMAP).key:
        return cfg.state / LEDGER
    return (cfg.state / LEDGER).with_suffix(f".{hashlib.sha256(key.encode()).hexdigest()[:12]}.json")


def _ledger(path: Path) -> dict[str, dict] | None:
    """The asks made so far, by pull request number; None for a file that is there and unusable."""
    try:
        reports = json.loads(path.read_text())["reports"]
    except FileNotFoundError:
        return {}
    except (OSError, ValueError, KeyError, TypeError):
        return None
    usable = isinstance(reports, dict) and all(
        isinstance(entry, dict)
        and type(entry.get("asks", 0)) is int
        and all(type(entry.get(key, 0)) in (int, float) for key in ("asked_at", "checked_at"))
        for entry in reports.values()
    )
    return reports if usable else None


def _gate_busy(roadmap: GitHub, account: str, since: float) -> bool | None:
    """Whether a run of the gate workflow that this account started at or after `since`, by opening
    a pull request or by dispatch, has yet to finish. None when the runs could not be read."""
    created = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(since - 60))
    query = urlencode({"actor": account, "created": f">={created}", "per_page": 100})
    answer = roadmap.api_jq(
        f"repos/{ROADMAP}/actions/workflows/{PROGRESS_MERGE_WORKFLOW}/runs?{query}", "[.workflow_runs[].status]"
    )
    try:
        statuses = json.loads(answer or "")
    except ValueError:
        return None
    return any(status != "completed" for status in statuses) if isinstance(statuses, list) else None


def _last_refusal(roadmap: GitHub, number: int) -> str:
    """The reason in the gate's newest refusal comment on the report, or ""."""
    for comment in reversed(roadmap.issue_comments(number) or []):
        body = str(comment.get("body") or "")
        if REFUSAL in body:
            fenced = body.split("```")
            return one_line(fenced[1] if len(fenced) > 2 else body, 500)
    return ""


def _dispatch(inputs: dict[str, object]):
    return run(
        [
            "gh",
            "api",
            "--method",
            "POST",
            f"repos/{ROADMAP}/actions/workflows/{PROGRESS_MERGE_WORKFLOW}/dispatches",
            "--input",
            "-",
        ],
        # The ledger entry written before this call is what keeps a retried round from asking
        # twice, so the outbox must not keep the request to replay it.
        env={**os.environ, "TAUCETI_OUTBOX_REPLAY": "1"},
        input_text=json.dumps({"ref": "main", "inputs": inputs}),
    )


def _ask(number: int) -> tuple[str, str]:
    """Dispatch the gate for one report: ("sent" | "unsent" | "rejected" | "unknown", gh's error).

    `unsent` is a request that a rate limit or an expired login stopped, `rejected` one GitHub will
    not accept from this account or for this workflow, and `unknown` one whose answer was lost."""
    # A dispatch counts as one by hand, which lands the report past a busy merge queue unless it
    # asks to wait. GitHub refuses an input the workflow does not declare, and a gate without this
    # one has no queue hold to keep, so it is asked again without it.
    result = _dispatch({"pr": str(number), "wait_for_queue": True})
    if result.returncode != 0 and "Unexpected inputs" in f"{result.stderr or ''}\n{result.stdout or ''}":
        result = _dispatch({"pr": str(number)})
    if result.returncode == 0:
        return "sent", ""
    error = one_line(result.stderr or result.stdout or f"gh exited {result.returncode}", 200)
    statuses = [record.get("status") or 0 for record in getattr(result, "github_records", [])]
    if (
        getattr(result, "github_before_request", False)
        or _gh_rate_kind(f"{result.stderr or ''}\n{result.stdout or ''}") is not None
        or any(status in (401, 429) for status in statuses)
    ):
        return "unsent", error
    if statuses and all(400 <= status < 500 for status in statuses):
        return "rejected", error
    return "unknown", error


def _hand_over(roadmap: GitHub, row: dict, entry: dict, label: str, now: float) -> None:
    """Stop asking about a report and tell a person: an incident record where the directory is
    configured, and one log line either way."""
    number, opened = int(row["number"]), _parse_iso8601(row.get("createdAt")) or now
    asks = int(entry.get("asks") or 0)
    if entry.get("rejected"):
        cause = f"this account could not dispatch {PROGRESS_MERGE_WORKFLOW} ({entry['rejected']})"
    else:
        cause = f"the merge gate has not landed it after {asks} more ask(s)"
    refusal = _last_refusal(roadmap, number)
    detail = f"{label} has been open {(now - opened) / 3600:.1f} h and {cause}; " + (
        f"the gate's last refusal: {refusal}" if refusal else "the gate left no refusal on the pull request"
    )
    if incidents.directory() is not None and not incidents.note_progress_stuck(
        ROADMAP,
        number,
        str(row.get("headRefOid") or ""),
        url=str(row.get("url") or f"https://github.com/{ROADMAP}/pull/{number}"),
        area=row["headRefName"].rsplit("/", 1)[-1],
        opened_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(opened)),
        asks=asks,
        refusal=refusal,
        detail=detail,
    ):
        entry["checked_at"] = now
        return
    entry["stuck"] = True
    warn_red(f"progress: {detail}. Not asking again; it stays open until it lands or someone closes it")


def _consider(roadmap: GitHub, account: str, row: dict, entry: dict, save) -> None:
    opened = _parse_iso8601(row.get("createdAt"))
    if row.get("isDraft") or entry.get("stuck") or opened is None:
        return
    now = time.time()
    asks = int(entry.get("asks") or 0)
    last = entry.get("asked_at") or opened
    wait = PROGRESS_SHEPHERD_INTERVAL if asks else PROGRESS_SHEPHERD_GRACE
    if now < max(last + wait, entry.get("checked_at", 0) + PROGRESS_SHEPHERD_RECHECK):
        return
    number = int(row["number"])
    label = f"report #{number} ({row['headRefName'].rsplit('/', 1)[-1]})"
    if incidents.progress_stuck(ROADMAP, number):
        entry["stuck"] = True
        return
    if not entry.get("rejected"):
        busy = _gate_busy(roadmap, account, last)
        if busy is not False:
            entry["checked_at"] = now
            why = "the gate's runs could not be read" if busy is None else "a gate run of this account is unfinished"
            log(f"progress: {label} is still open and {why}; looking again later")
            return
    if asks >= PROGRESS_SHEPHERD_ASKS or entry.get("rejected"):
        _hand_over(roadmap, row, entry, label, now)
        return
    previous = dict(entry)
    entry.update(asks=asks + 1, asked_at=now)
    entry.pop("checked_at", None)
    save()
    outcome, error = _ask(number)
    if outcome == "sent":
        log(f"progress: asked the merge gate about {label} again ({asks + 1}/{PROGRESS_SHEPHERD_ASKS})")
    elif outcome == "unsent":
        entry.clear()
        entry.update(previous, checked_at=now)
        log(f"progress: could not ask the merge gate about {label} yet ({error})")
    elif outcome == "rejected":
        entry.clear()
        entry.update(previous, rejected=error)
        _hand_over(roadmap, row, entry, label, now)
    else:
        log(f"progress: asking the merge gate about {label} gave no clear answer ({error}); counted as asked")


def look(cfg) -> list[dict] | None:
    """One pass over this account's open progress reports in the roadmap repository. Returns their
    rows (`number` and `headRefName` among the fields), or None when they could not be listed."""
    account = me()
    roadmap = GitHub(ROADMAP)
    try:
        reports = progress_reports(FIELDS, account)
    except GitHubError as exc:
        log(f"progress: could not list this account's open reports ({one_line(str(exc), 120)}); asking about none")
        return None
    path = ledger_path(cfg)
    before = _ledger(path)
    if before is None:
        log(f"progress: {path} is unreadable, so no open report is asked about; delete it to start the count again")
        return reports
    ledger = {str(row["number"]): dict(before.get(str(row["number"])) or {}) for row in reports}

    def noted():
        return {number: entry for number, entry in ledger.items() if entry}

    def save():
        transport.atomic(path, {"version": 1, "reports": noted()})

    try:
        try:
            for row in reports:
                _consider(roadmap, account, row, ledger[str(row["number"])], save)
        finally:
            if noted() != before:
                save()
    except OSError as exc:
        log(f"progress: the pass over open reports stopped early ({type(exc).__name__}: {exc})")
    return reports

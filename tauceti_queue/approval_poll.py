"""The approval reconciliation poll: approvals and revocations a webhook delivery may have missed.

TauCetiReview approves by publishing a `merge eligibility` check run (`tauceti_queue.eligibility`);
the webhook receiver reconciles each `check_run` delivery. A delivery can be lost (the App webhook
inactive, the service down longer than GitHub redelivers, a tunnel outage), and a lost approval would
leave a PR waiting for nothing while a lost revocation would land a PR whose review was withdrawn.
`ApprovalPoller.run_once` is the bounded periodic pass behind the webhooks: it lists the open PRs
with their eligibility check runs (`GitHubPort.list_open_pull_requests`, one GraphQL page per 100
PRs on the production adapter), and feeds through `eligibility.reconcile`, the same path as a
delivery, exactly those whose decision the controller has not recorded: an eligible check at a head
the controller holds no live input for, or an unsafe review on a PR it does. A waiting input whose
PR moved to another head, was closed or became a draft is told the way the webhook path tells it
(`controller.handle_event` with the equivalent `pull_request` action); a member PR is journaled and
left alone, as the webhook path does. A decision the controller already consumed is not applied
again: an input cancelled at the same head (withdrawn by `bors r-`, conflicting with the canonical
branch, landed by other means, withdrawn as `problem-anticipated`) stays cancelled until a newer check (a higher id) decides again, as
it would under the webhook path, which only acts on new deliveries.

Budget: one listing (GraphQL, ~12 calls for 1,200 open PRs) plus, per difference found, the calls
`eligibility.reconcile` makes (a PR read, the check runs, the merge base: at most four REST calls),
plus one PR read per waiting input absent from the listing. Nothing runs while the remaining REST
quota is below `reserve`, and a pass stops reconciling when it falls below it mid-way; the skipped
work is picked up by the next pass. The pass records its summary through
`controller.record_approval_poll`, and every approval it (rather than a webhook) discovered counts
in `status.approvals.polled`.

`ApprovalPollLoop` runs a pass at service start and every `approval_poll_interval_seconds` (a
runtime setting), and on demand for `tauceti-queue reconcile-approvals`.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import Callable
from typing import Any

from . import eligibility
from .commands import live_pull_request

log = logging.getLogger("tauceti_queue.approval_poll")

LIVE = ("waiting", "reserved", "inserted")
WAITING = ("waiting", "reserved")
# Dispositions that cannot change without a new check (a new check id): the same decision is not retried.
SETTLED = frozenset({"approved", "revoked", "moved", "waiting", "absent"})


class ApprovalPoller:
    def __init__(
        self,
        controller,
        adapter,
        repo: str,
        *,
        review_app_id: int = eligibility.REVIEW_APP_ID,
        reserve: int = 200,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.controller = controller
        self.adapter = adapter
        self.repo = repo
        self.review_app_id = review_app_id
        self.reserve = reserve
        self.clock = clock
        self.passes = 0
        self.skipped = 0
        self.last_summary: dict[str, Any] | None = None
        self._applied: dict[int, tuple[str, int, str]] = {}
        self._lock = threading.Lock()

    # -- scheduling

    def interval(self) -> float:
        return float(self.controller.setting("approval_poll_interval_seconds"))

    def due(self, now: float | None = None) -> bool:
        now = self.clock() if now is None else now
        last = (self.controller.status().get("approvals") or {}).get("last_poll") or {}
        at = last.get("at")
        return at is None or now - float(at) >= self.interval()

    # -- the pass

    def run_once(self, *, reason: str = "interval") -> dict[str, Any]:
        with self._lock:
            return self._run(reason)

    def _budget_ok(self) -> bool:
        try:
            remaining, _reset = self.adapter.rate_limit()
        except Exception as error:  # noqa: BLE001 - quota unknown: proceed, the calls report their own failures
            log.debug("rate limit unavailable: %s", error)
            return True
        return remaining is None or remaining >= self.reserve

    def _run(self, reason: str) -> dict[str, Any]:
        started = self.clock()
        summary: dict[str, Any] = {
            "at": started,
            "reason": reason,
            "listed": 0,
            "with_check": 0,
            "reconciled": 0,
            "approved": 0,
            "revoked": 0,
            "head_changed": 0,
            "closed": 0,
            "drafted": 0,
            "member_events": 0,
            "unchanged": 0,
            "skipped": False,
            "stopped_early": False,
            "errors": [],
        }
        if not self._budget_ok():
            summary["skipped"] = True
            self.skipped += 1
            self._finish(summary, started)
            return summary
        try:
            listed = self.adapter.list_open_pull_requests(self.repo, review_app_id=self.review_app_id)
        except Exception as error:  # noqa: BLE001 - the pass reports the failure; the next one retries
            summary["errors"].append(f"listing failed: {error}")
            self._finish(summary, started)
            return summary
        summary["listed"] = len(listed)
        numbers = set()
        for pull in listed:
            numbers.add(pull.number)
            try:
                self._one(pull, summary)
            except Exception as error:  # noqa: BLE001 - one PR's failure must not end the pass
                summary["errors"].append(f"#{pull.number}: {error}")
            if summary["stopped_early"]:
                break
        if not summary["stopped_early"]:
            self._absent_waiting(numbers, summary)
        self.passes += 1
        self._finish(summary, started)
        return summary

    def _finish(self, summary: dict[str, Any], started: float) -> None:
        summary["duration_seconds"] = round(self.clock() - started, 3)
        summary["errors"] = summary["errors"][:20]
        self.last_summary = summary
        self.controller.record_approval_poll(summary)
        log.info(
            "approval poll (%s): listed %d, with check %d, approved %d, revoked %d, head changed %d, closed %d%s",
            summary["reason"],
            summary["listed"],
            summary["with_check"],
            summary["approved"],
            summary["revoked"],
            summary["head_changed"],
            summary["closed"],
            " (skipped: quota)" if summary["skipped"] else "",
        )

    def _event(self, number: int, action: str, pull: dict[str, Any]) -> None:
        payload = {
            "action": action,
            "number": number,
            "pull_request": dict(pull, number=number),
            "repository": {"full_name": self.repo},
            "sender": {"login": "tauceti-queue approval poll"},
        }
        self.controller.handle_event("pull_request", payload)

    def _one(self, pull, summary: dict[str, Any]) -> None:
        row = self.controller.input_for(self.repo, pull.number)
        live = row is not None and row["state"] in LIVE
        if live and row["head_sha"] != pull.head_sha:
            key = (pull.head_sha, 0, "head-changed")
            if self._applied.get(pull.number) != key:
                # The webhook path's `synchronize`: a waiting input is withdrawn, a member is journaled and kept.
                self._event(pull.number, "synchronize", {"head": {"sha": pull.head_sha}})
                self._applied[pull.number] = key
                if row["state"] in WAITING:
                    summary["head_changed"] += 1
                else:
                    summary["member_events"] += 1
            row = self.controller.input_for(self.repo, pull.number)
            live = row is not None and row["state"] in LIVE
        if live and pull.draft and row["state"] in WAITING:
            self._event(pull.number, "converted_to_draft", {"draft": True})
            summary["drafted"] += 1
            row = self.controller.input_for(self.repo, pull.number)
            live = row is not None and row["state"] in LIVE
        checks = list(pull.eligibility_checks)
        if not checks:
            return
        summary["with_check"] += 1
        decision = eligibility.latest(checks, self.repo, pull.number, pull.head_sha, self.review_app_id)
        if decision is None:
            return
        key = (pull.head_sha, decision.check_id, "decision")
        if decision.eligible:
            if live and row["head_sha"] == pull.head_sha:
                summary["unchanged"] += 1
                return
            if row is not None and row["head_sha"] == pull.head_sha and _consumed(row, decision):
                return
        elif not decision.review_safe:
            if row is None or row["state"] not in LIVE:
                return
        else:
            return
        if self._applied.get(pull.number) == key:
            return
        if not self._budget_ok():
            summary["stopped_early"] = True
            return
        disposition = eligibility.reconcile(
            self.controller, self.adapter, self.repo, pull.number, review_app_id=self.review_app_id
        )
        summary["reconciled"] += 1
        if disposition in SETTLED:
            self._applied[pull.number] = key
        if disposition == "approved":
            summary["approved"] += 1
            self.controller.note_approval("polled")
        elif disposition == "revoked":
            if row is not None and row["state"] in WAITING:
                summary["revoked"] += 1
            else:
                summary["member_events"] += 1

    def _absent_waiting(self, listed: set[int], summary: dict[str, Any]) -> None:
        """Waiting inputs whose PR is not among the open PRs: closed, or the listing is not the whole truth; read each."""
        for row in self.controller.inputs(["waiting", "reserved"]):
            number = int(row["number"])
            if number in listed:
                continue
            if not self._budget_ok():
                summary["stopped_early"] = True
                return
            pull = live_pull_request(self.adapter, self.repo, number)
            if pull is None or pull.state != "open":
                merged = pull is not None and pull.title.startswith("[Merged by Bors] - ")
                self._event(number, "closed", {"merged": merged, "title": pull.title if pull else ""})
                summary["closed"] += 1
            elif pull.head_sha != row["head_sha"]:
                self._event(number, "synchronize", {"head": {"sha": pull.head_sha}})
                summary["head_changed"] += 1
            elif pull.draft:
                self._event(number, "converted_to_draft", {"draft": True})
                summary["drafted"] += 1


def _consumed(row: dict[str, Any], decision: eligibility.Decision) -> bool:
    """Whether the input row at the decision's head already answers for it (landed, or cancelled with no newer check)."""
    if row["state"] == "landed":
        return True
    if row["state"] != "cancelled":
        return False
    try:
        evidence = json.loads(row.get("evidence") or "{}")
    except ValueError:
        evidence = {}
    if evidence.get("kind") == "eligibility_check" and isinstance(evidence.get("comment_id"), int):
        return evidence["comment_id"] >= decision.check_id
    return row.get("reason") in (
        "unapproved",
        "cancelled",
        "conflicts-with-main",
        "landed-externally",
        "problem-anticipated",
    )


class ApprovalPollLoop:
    """A thread running `poller.run_once()` at start, every `approval_poll_interval_seconds`, and on `wake()`."""

    def __init__(self, poller: ApprovalPoller, *, max_interval: float | None = None) -> None:
        self.poller = poller
        self.max_interval = max_interval
        self.stop_event = threading.Event()
        self.wake_event = threading.Event()
        self._thread: threading.Thread | None = None
        if hasattr(poller, "wake"):
            poller.wake = self.wake
        if hasattr(poller, "stopping"):
            poller.stopping = self.stop_event

    def interval(self) -> float:
        interval = self.poller.interval()
        if self.max_interval is not None:
            interval = min(interval, self.max_interval)
        return max(interval, 0.05)

    def run(self) -> None:
        reason = "start"
        while not self.stop_event.is_set():
            self.wake_event.clear()
            try:
                self.poller.run_once(reason=reason)
            except Exception:  # noqa: BLE001 - the loop must survive any adapter failure
                log.exception("approval poll failed")
            woken = self.wake_event.wait(timeout=self.interval())
            reason = "wake" if woken else "interval"

    def wake(self) -> None:
        self.wake_event.set()

    def start(self) -> threading.Thread:
        self._thread = threading.Thread(target=self.run, name="tauceti-queue-approval-poll", daemon=True)
        self._thread.start()
        return self._thread

    def stop(self, timeout: float | None = 10) -> None:
        self.stop_event.set()
        self.wake_event.set()
        if self._thread is not None:
            self._thread.join(timeout)

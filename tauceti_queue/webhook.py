"""GitHub webhook verification and routing for the AI merge queue.

`verify_signature` checks the `X-Hub-Signature-256` HMAC that GitHub computes with the App's
webhook secret over the raw request body. `WebhookReceiver.handle` then routes one delivery:

- `issue_comment` (created, on a pull request) and `pull_request_review` (submitted, with a body)
  are read for Bors commands by `commands.run_comment`; the review App's exact-head comment
  approvals are recognised by the issuing App's id, as Bors recognises them;
- `check_run` deliveries of the review App's `merge eligibility` check, the way TauCetiReview
  approves today, are reconciled by `eligibility.reconcile`;
- every subscribed event (`pull_request`, `pull_request_review`, `issue_comment`, `status`,
  `check_run`, `check_suite`, `push`, `workflow_run`) is passed to `controller.handle_event(kind,
  payload)` so the controller can bind statuses and runs to its candidates.

Deliveries are deduplicated by `X-GitHub-Delivery` over a bounded window, because GitHub redelivers
on timeout and a duplicated approval must not become two admissions.
"""

from __future__ import annotations

import collections
import dataclasses
import hashlib
import hmac
import logging
import threading
from collections.abc import Iterable

from . import commands, eligibility

log = logging.getLogger("tauceti_queue.webhook")

EVENTS = (
    "pull_request",
    "pull_request_review",
    "issue_comment",
    "status",
    "check_run",
    "check_suite",
    "push",
    "workflow_run",
)
# The review App that approves on TauCetiReview's behalf (bors-ng deploy/cloudflare/README.md).
REVIEW_APP_ID = 3947238


def signature_for(secret: bytes, body: bytes) -> str:
    return "sha256=" + hmac.new(secret, body, hashlib.sha256).hexdigest()


def verify_signature(secret: bytes, body: bytes, header: str | None) -> bool:
    if not header or not header.startswith("sha256="):
        return False
    return hmac.compare_digest(signature_for(secret, body), header.strip())


@dataclasses.dataclass
class Delivery:
    event: str
    delivery_id: str
    payload: dict


class WebhookReceiver:
    def __init__(
        self,
        controller,
        adapter,
        *,
        repositories: Iterable[str],
        review_app_id: int | None = REVIEW_APP_ID,
        dedupe_window: int = 4096,
    ) -> None:
        self.controller = controller
        self.adapter = adapter
        self.repositories = set(repositories)
        self.review_app_id = review_app_id
        self._seen: collections.OrderedDict[str, None] = collections.OrderedDict()
        self._window = dedupe_window
        self._lock = threading.Lock()
        self.received = 0
        self.duplicates = 0
        self.ignored = 0

    def _duplicate(self, delivery_id: str) -> bool:
        if not delivery_id:
            return False
        with self._lock:
            if delivery_id in self._seen:
                return True
            self._seen[delivery_id] = None
            while len(self._seen) > self._window:
                self._seen.popitem(last=False)
            return False

    def handle(self, delivery: Delivery) -> str:
        """Route one verified delivery; returns a short disposition for the HTTP reply and logs."""
        self.received += 1
        if self._duplicate(delivery.delivery_id):
            self.duplicates += 1
            return "duplicate"
        if delivery.event == "ping":
            return "pong"
        payload = delivery.payload
        repo = str(((payload.get("repository") or {}).get("full_name")) or "")
        if delivery.event not in EVENTS or repo not in self.repositories:
            self.ignored += 1
            return "ignored"
        admission = getattr(self.controller, "admission", None)
        if admission is not None:
            try:
                admission.handle_event(delivery.event, payload)
            except BaseException:
                # A failed durable invalidation must be retried on redelivery, not remembered
                # as successfully handled by the in-memory delivery optimization.
                with self._lock:
                    self._seen.pop(delivery.delivery_id, None)
                raise
        if delivery.event == "issue_comment":
            self._issue_comment(repo, payload)
        elif delivery.event == "pull_request_review":
            self._review(repo, payload)
        elif delivery.event == "check_run":
            self._check_run(repo, payload)
        self.controller.handle_event(delivery.event, payload)
        return "ok"

    def _check_run(self, repo: str, payload: dict) -> None:
        """A `merge eligibility` check from the review App is the App's approval or revocation."""
        if getattr(self.controller, "admission", None) is not None:
            return  # late legacy publications have no automatic authority after the switch
        pr = eligibility.notification_pr(payload.get("check_run") or {}, repo, review_app_id=self.review_app_id)
        if pr is not None:
            disposition = eligibility.reconcile(
                self.controller, self.adapter, repo, pr, review_app_id=self.review_app_id
            )
            note = getattr(self.controller, "note_approval", None)
            if disposition == "approved" and callable(note):
                note("webhook")

    def _issue_comment(self, repo: str, payload: dict) -> None:
        if payload.get("action") != "created":
            return
        issue = payload.get("issue") or {}
        if "pull_request" not in issue:
            return
        comment = payload.get("comment") or {}
        user = comment.get("user") or {}
        app = comment.get("performed_via_github_app") or {}
        review_app = bool(self.review_app_id) and app.get("id") == self.review_app_id
        if review_app and getattr(self.controller, "admission", None) is not None:
            return  # retired automatic exact-head commands cannot compete with primary intake
        context = commands.CommentContext(
            repo=repo,
            pr_number=int(issue.get("number") or 0),
            actor_login=str(user.get("login") or ""),
            actor_id=int(user.get("id") or 0),
            body=str(comment.get("body") or ""),
            created_at=str(comment.get("created_at") or ""),
            comment_id=int(comment["id"]) if comment.get("id") else None,
            comment_url=str(comment.get("html_url") or ""),
            kind="issue_comment",
            review_app=review_app,
        )
        if not context.pr_number or not context.actor_login:
            return
        commands.run_comment(self.controller, self.adapter, context)

    def _review(self, repo: str, payload: dict) -> None:
        if payload.get("action") != "submitted":
            return
        review = payload.get("review") or {}
        pr = payload.get("pull_request") or {}
        user = review.get("user") or {}
        context = commands.CommentContext(
            repo=repo,
            pr_number=int(pr.get("number") or 0),
            actor_login=str(user.get("login") or ""),
            actor_id=int(user.get("id") or 0),
            body=str(review.get("body") or ""),
            created_at=str(review.get("submitted_at") or ""),
            comment_id=int(review["id"]) if review.get("id") else None,
            comment_url=str(review.get("html_url") or ""),
            kind="pull_request_review",
            review_head=str(review.get("commit_id") or "") or None,
        )
        if not context.pr_number or not context.actor_login or not context.body:
            return
        commands.run_comment(self.controller, self.adapter, context)

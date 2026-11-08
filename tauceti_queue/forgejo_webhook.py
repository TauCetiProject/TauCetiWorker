"""Verified native source notifications invalidate observations, never grant authority."""

from __future__ import annotations

import collections
import hashlib
import hmac
import re
import threading

from tauceti_tools.ci_candidate import RepositoryIdentity, read_json

from .admission import relevant_comment


class ForgejoWebhookReceiver:
    def __init__(self, controller, source, secret, *, dedupe_window=4096):
        if (
            not isinstance(source, RepositoryIdentity)
            or source.repository.forge.kind != "forgejo"
            or not isinstance(secret, bytes)
            or len(secret) < 32
            or type(dedupe_window) is not int
            or not 1 <= dedupe_window <= 65536
        ):
            raise ValueError("native webhook needs its qualified source and dedicated secret")
        self.controller, self.source, self._secret = controller, source, secret
        self._seen, self._window = collections.OrderedDict(), dedupe_window
        self._lock = threading.Lock()
        self.received = self.duplicates = self.ignored = 0

    def handle(self, body, *, event, delivery, signature):
        # Forgejo sends a raw hexadecimal SHA-256 digest, not GitHub's prefix.
        if (
            not isinstance(signature, str)
            or re.fullmatch(r"[0-9a-f]{64}", signature) is None
            or not hmac.compare_digest(hmac.new(self._secret, body, hashlib.sha256).hexdigest(), signature)
        ):
            raise PermissionError("invalid native webhook signature")
        if not isinstance(delivery, str) or re.fullmatch(r"[A-Za-z0-9-]{1,128}", delivery) is None:
            raise ValueError("native webhook needs a bounded delivery identity")
        payload = read_json(body, max_bytes=1024 * 1024)
        if not isinstance(payload, dict):
            raise ValueError("native webhook must be an object")
        repo = payload.get("repository")
        if (
            not isinstance(repo, dict)
            or type(repo.get("id")) is not int
            or repo["id"] != self.source.native_id
            or repo.get("full_name") != self.source.repository.name
            or not isinstance(repo.get("html_url"), str)
            or repo.get("html_url", "").rstrip("/") != self.source.repository.url.rstrip("/")
        ):
            raise PermissionError("native webhook belongs to another source repository")
        with self._lock:
            self.received += 1
            identity = (delivery, event, hashlib.sha256(body).hexdigest())
            previous = self._seen.get(delivery)
            if previous is not None:
                if previous != identity:
                    raise ValueError("native delivery identity was reused with different contents")
                self.duplicates += 1
                return "duplicate"
            poller = getattr(self.controller, "admission", None)
            if poller is None:
                raise RuntimeError("native primary admission has not been wired")
            # Store invalidation before acknowledging. A crash before remembering
            # the delivery only repeats an idempotent invalidation after restart.
            # No command parsing, review evidence, ACL grant or payload mutation.
            if event == "push":
                with self.controller.store.transaction():
                    saved = self.controller.store.get_setting("state:admission_discovery", {})
                    self.controller.store.set_setting(
                        "state:admission_discovery", {**saved, "retry_at": 0, "next_attempt_at": 0}
                    )
                if payload.get("ref") == "refs/heads/" + self.controller.canonical_branch:
                    poller.handle_event("push", payload)
                else:
                    poller.request_discovery()
            elif event.startswith("pull_request") or event == "issue_comment":
                pull = payload.get("pull_request") or {}
                issue = payload.get("issue") or {}
                if not isinstance(pull, dict) or not isinstance(issue, dict):
                    raise ValueError("native pull notification is malformed")
                number = payload.get("number") or pull.get("number") or issue.get("number")
                if event == "issue_comment" and payload.get("is_pull") is not True and not pull:
                    self.ignored += 1
                    return "ignored"
                if type(number) is not int or not 0 < number < 2**63:
                    raise ValueError("native pull event has no positive pull number")
                endpoint = pull.get("head") or {}
                if not isinstance(endpoint, dict):
                    raise ValueError("native pull head is malformed")
                head = endpoint.get("sha", "")
                if head and (not isinstance(head, str) or re.fullmatch(r"[0-9a-f]{40}", head) is None):
                    raise ValueError("native pull event head is malformed")
                sender = payload.get("sender") or {}
                if not isinstance(sender, dict):
                    raise ValueError("native notification sender is malformed")
                sender_id = sender.get("id")
                controller_actor = getattr(self.controller, "source_actor", None)
                own_output = controller_actor is not None and str(sender_id) == controller_actor.principal.native_id
                comment_event = event in {"issue_comment", "pull_request_comment"} and "comment" in payload
                action = payload.get("action")
                if own_output and (
                    comment_event or action in {"label_updated", "label_cleared", "labeled", "unlabeled"}
                ):
                    self.ignored += 1
                    return "ignored"
                if comment_event:
                    comment = payload.get("comment") or {}
                    if not isinstance(comment, dict):
                        raise ValueError("native comment is malformed")
                    changes = payload.get("changes") or {}
                    previous = ((changes.get("body") or {}).get("from") or "") if isinstance(changes, dict) else ""
                    if (
                        action != "deleted"
                        and not relevant_comment(comment)
                        and not relevant_comment({"body": previous})
                    ):
                        self.ignored += 1
                        return "ignored"
                    config = getattr(getattr(self.controller.source, "admission_policy", None), "publishers", None)
                    publishers = set() if config is None else config.reviews | config.recoveries
                    urgent = any(str(sender_id) == p.native_id for p in publishers)
                    poller.invalidate(number, head=head, urgent=urgent)
                else:
                    if action == "edited" and payload.get("changes") and set(payload["changes"]) <= {"title"}:
                        self.ignored += 1
                        return "ignored"
                    normalized = dict(payload)
                    if action == "label_updated":
                        normalized["action"] = "labeled"
                    elif action == "label_cleared":
                        normalized["action"] = "unlabeled"
                    poller.handle_event("pull_request", normalized)
            elif event == "ping":
                return "pong"
            else:
                self.ignored += 1
                return "ignored"
            self._seen[delivery] = identity
            while len(self._seen) > self._window:
                self._seen.popitem(last=False)
        return "invalidated"

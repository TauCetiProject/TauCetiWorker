"""Verified native source notifications invalidate observations, never grant authority."""

from __future__ import annotations

import collections
import hashlib
import hmac
import re
import threading

from tauceti_tools.ci_candidate import RepositoryIdentity, read_json


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
                if payload.get("ref") == "refs/heads/" + self.controller.canonical_branch:
                    poller.handle_event("push", payload)
                else:
                    poller.request_discovery()
            elif event.startswith("pull_request") or event == "issue_comment":
                pull = payload.get("pull_request") or {}
                issue = payload.get("issue") or {}
                number = payload.get("number") or pull.get("number") or issue.get("number")
                if event == "issue_comment" and payload.get("is_pull") is not True and not pull:
                    self.ignored += 1
                    return "ignored"
                if type(number) is not int or not 0 < number < 2**63:
                    raise ValueError("native pull event has no positive pull number")
                head = (pull.get("head") or {}).get("sha", "")
                if head and (not isinstance(head, str) or re.fullmatch(r"[0-9a-f]{40}", head) is None):
                    raise ValueError("native pull event head is malformed")
                poller.invalidate(number, head=head)
            elif event == "ping":
                return "pong"
            else:
                self.ignored += 1
                return "ignored"
            self._seen[delivery] = identity
            while len(self._seen) > self._window:
                self._seen.popitem(last=False)
        return "invalidated"

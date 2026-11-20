"""Operator notifications: the queue's direct messages about its automatic infrastructure backoff.

The controller never removes a member of the head on its own: a failing head goes to repair however
often it fails, and only a repair agent that reduces a member's contribution to an empty commit
withdraws that member (`docs/queue/escalation.md`). What it does tell the operator about is the
start and end of an infrastructure backoff, as a status-page alert and as a Zulip direct message
through the controller's outbox (kind `notify`), so a notification survives restarts and is retried
until delivered, and is sent at most once. `ZulipNotifier` reuses the security audit's Zulip client
(`tauceti_worker.audit_alerts.request`: HTTPS origin only, no redirects, bot basic auth) and its
configuration format, a mode-0600 JSON file:

    {"site": "https://leanprover.zulipchat.com", "email": "BOT_EMAIL", "api_key": "BOT_API_KEY", "recipient_id": 123}

named by `TAUCETI_QUEUE_ZULIP_CONFIG` in the service's environment. Without it the outbox entry
completes as `notifier-unconfigured` and only the status alert remains.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Protocol

ZULIP_CONFIG_ENV = "TAUCETI_QUEUE_ZULIP_CONFIG"


class Notifier(Protocol):
    def send(self, marker: str, content: str, *, reconcile: bool) -> str: ...


class ZulipNotifier:
    """Direct messages to one Zulip user from a bot, reconciled by a marker before any re-send."""

    def __init__(self, config: Mapping[str, Any]) -> None:
        for key in ("site", "email", "api_key", "recipient_id"):
            if key not in config:
                raise ValueError(f"Zulip configuration lacks {key}")
        self.config = dict(config)

    @classmethod
    def from_file(cls, path: str | os.PathLike[str]) -> ZulipNotifier:
        return cls(json.loads(Path(path).read_text()))

    def _request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        from tauceti_worker.audit_alerts import request

        return request(self.config, method, params)

    def send(self, marker: str, content: str, *, reconcile: bool) -> str:
        """Send `content` (which carries `marker`) unless an earlier attempt already delivered it: an
        earlier POST may have succeeded before its response was lost, so a retry (`reconcile`) first
        looks for the marker among the bot's recent messages in this conversation."""
        recipient = self.config["recipient_id"]
        if reconcile:
            recent = self._request(
                "GET",
                {
                    "narrow": json.dumps([{"operator": "dm", "operand": [recipient]}]),
                    "anchor": "newest",
                    "num_before": 100,
                    "num_after": 0,
                    "apply_markdown": "false",
                },
            )
            found = next((m for m in recent.get("messages", []) if marker in m.get("content", "")), None)
            if found is not None:
                return f"zulip {found['id']} (reconciled)"
        result = self._request("POST", {"type": "direct", "to": json.dumps([recipient]), "content": content})
        return f"zulip {result['id']}"


def notifier_from_environment(environ: Mapping[str, str] | None = None) -> ZulipNotifier | None:
    """The notifier `TAUCETI_QUEUE_ZULIP_CONFIG` names, or None when it is unset."""
    environ = os.environ if environ is None else environ
    path = environ.get(ZULIP_CONFIG_ENV)
    return ZulipNotifier.from_file(path) if path else None


def quote(text: str, limit: int = 4000) -> str:
    """Untrusted text (diagnostics, agent notes) as inert Zulip content: no mentions, no code fences."""
    return text[:limit].replace("`", "ˋ").replace("@", "＠")

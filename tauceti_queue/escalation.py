"""Failure fingerprints and operator notifications for the queue's loop-termination guarantee.

The controller never re-dispatches an unchanged candidate after the same failure: every source
failure is followed by an adopted repair, a withdrawal, a split (bisection) or an alert. To tell "the
same failure again" from a new one it fingerprints each failed CI attempt (`fingerprint`): the verdict
class (which verdict contexts failed, and for verified source CI its classification), the members the
findings name, if any, and a digest of the findings' key lines with locations, commit SHAs and URLs
normalised away, so that a repair that only moved a failing line does not make a failure look new.

Escalations are told to the operator twice: as a status-page alert, and as a Zulip direct message
through the controller's outbox (kind `notify`), so a notification survives restarts and is retried
until delivered, and is sent at most once. `ZulipNotifier` reuses the security audit's Zulip client
(`tauceti_worker.audit_alerts.request`: HTTPS origin only, no redirects, bot basic auth) and its
configuration format, a mode-0600 JSON file:

    {"site": "https://leanprover.zulipchat.com", "email": "BOT_EMAIL", "api_key": "BOT_API_KEY", "recipient_id": 123}

named by `TAUCETI_QUEUE_ZULIP_CONFIG` in the service's environment. Without it the outbox entry
completes as `notifier-unconfigured` and only the status alert remains.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any, Protocol

ZULIP_CONFIG_ENV = "TAUCETI_QUEUE_ZULIP_CONFIG"
# Finding kinds that describe the run rather than the source; they never enter a fingerprint.
NON_SOURCE_KINDS = frozenset({"infrastructure", "admission_predictions", "cross_batch", "prepare_errors"})
# Verdict states that are not failures.
PASSING_STATES = frozenset({"success", "not_required", "absent", "unbound", "pending"})
# The most finding lines a fingerprint record keeps for display (the digest covers them all).
RECORDED_LINES = 20

_ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_LOCATION = re.compile(r":\d+:\d+")
_SHA = re.compile(r"\b[0-9a-f]{7,64}\b")
_URL = re.compile(r"https?://\S+")
_SPACE = re.compile(r"\s+")


def normalise(text: str) -> str:
    """A finding line without what changes when nothing about the failure has: colour codes, line and
    column numbers, commit SHAs, URLs and runs of whitespace."""
    text = _ANSI.sub("", text)
    text = _URL.sub("<url>", text)
    text = _LOCATION.sub(":<line>", text)
    text = _SHA.sub("<sha>", text)
    return _SPACE.sub(" ", text).strip()


def failing_contexts(verdicts: Mapping[str, Any]) -> list[str]:
    """The verdict contexts of an attempt that did not pass, sorted."""
    failed = []
    for context, verdict in verdicts.items():
        state = verdict.get("state") if isinstance(verdict, Mapping) else verdict
        if isinstance(state, str) and state not in PASSING_STATES:
            failed.append(context)
    return sorted(failed)


def verdict_class(outcome_detail: str, verdicts: Mapping[str, Any]) -> str:
    """`policy_failure:scope`, `source_failure:build`, `build`, `scope`: the classification verified
    source CI reported (when it did) and the contexts that failed."""
    parts = []
    prefix = "verified source CI: "
    if outcome_detail.startswith(prefix):
        parts.append(outcome_detail[len(prefix) :].split()[0])
    contexts = failing_contexts(verdicts) or ["build"]
    return ":".join(parts + [",".join(contexts)])


def named_members(diagnostics: Iterable[Any]) -> list[Any]:
    """The member references the findings carry (`source_pr`, `source_prs`, `pr`, `number`), unresolved."""
    named: list[Any] = []
    for finding in diagnostics:
        if not isinstance(finding, Mapping) or finding.get("kind") in NON_SOURCE_KINDS:
            continue
        if finding.get("kind") == "policy" and finding.get("member_head"):
            continue  # named by accepted head and source (`attribution.policy_references`), not by number
        values = list(finding.get("source_prs") or [])
        values += [finding.get(key) for key in ("source_pr", "pr", "number")]
        named += [value for value in values if value not in (None, "")]
    return named


def finding_lines(diagnostics: Iterable[Any]) -> list[str]:
    """The normalised `kind|module|message` line of every source finding, sorted and deduplicated."""
    lines = set()
    for finding in diagnostics:
        if not isinstance(finding, Mapping) or finding.get("kind") in NON_SOURCE_KINDS:
            continue
        lines.add(
            normalise(
                "|".join(str(finding.get(key) or "") for key in ("kind", "context", "module", "declaration", "message"))
            )
        )
    return sorted(lines)


def fingerprint(cls: str, members: Iterable[str], lines: Iterable[str]) -> dict[str, Any]:
    """The failure's identity: `fingerprint` hashes the class, the attributed members and `digest`, the
    hash of every finding line."""
    lines = sorted(lines)
    members = sorted(set(members))
    digest = hashlib.sha256("\n".join(lines).encode()).hexdigest()[:16]
    identity = hashlib.sha256(json.dumps([cls, members, digest]).encode()).hexdigest()[:16]
    return {
        "fingerprint": identity,
        "verdict_class": cls,
        "members": members,
        "digest": digest,
        "lines": lines[:RECORDED_LINES],
        "line_count": len(lines),
    }


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

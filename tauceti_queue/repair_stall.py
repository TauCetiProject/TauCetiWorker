"""No-progress detection for the head's repair loop (docs/queue/escalation.md, "Stalled repair").

A repair task whose claims keep ending as checkpoints at the same tip with the same findings is
making no progress: the next claim gets the same inputs and will most likely do the same thing.
Batch 465's repair task 1415 did that 47 times in about 12 hours, because the findings came from a
tooling failure no source change could fix, and every agent session said so in its final message.

This module holds the pure parts of the rule. The controller (`Controller._observe_repair_stall`)
applies it at each checkpoint, backs the task off and raises the alert:

- `signature` is the failure signature of one checkpoint: its tip and its findings, each reduced to
  the failing check (kind and check name), where it points (module or file) and its message with
  volatile detail (hashes, numbers, temporary paths) removed. Two checkpoints with equal signatures
  are the same failure on the same chain. The worker and TauCetiTools revisions are not part of it:
  a code change that leaves the failure as it was has not made progress either. They decide instead
  whether a backed-off task may be offered again at once (`Controller._claimable_tasks`).
- `conclusion` is what the agent itself concluded in its last message, cut to the passage that says
  why it could not make progress (for example that a human has to intervene).
- `retry_delay` is the backoff: `first` seconds, doubling at each further identical checkpoint up to
  `most`.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

TIMEZONE = ZoneInfo("Australia/Sydney")
# The status alert's wording, which the public observation recognises (`monitoring.py`).
ALERT_TEXT = "repair stalled"
HEAD_ALERT_TEXT = "made no progress"
# What a signature keeps of one finding's message, and how much of the agent's conclusion an alert quotes.
MESSAGE_LIMIT = 500
CONCLUSION_LIMIT = 700

_SHA = re.compile(r"\b[0-9a-f]{7,64}\b")
_TEMP_PATH = re.compile(r"(?:/tmp|/var/tmp|/private/tmp)/\S*")
_NUMBER = re.compile(r"\d+(?:\.\d+)?")
_SPACE = re.compile(r"\s+")
# Sentences of the agent's last message that explain why it cannot make progress.
_NEEDS_HUMAN = re.compile(
    r"\b(human|operator|manual(?:ly)?|intervene|intervention|cannot be fixed|can't be fixed|"
    r"no (?:content |source )?change|nothing (?:I|the agent) can|has to be split|must be split|"
    r"needs? to be fixed|outside (?:the|my) (?:scope|control)|human-owned)\b",
    re.IGNORECASE,
)


def normalize(message: str) -> str:
    """A finding's message without what varies between identical runs: commit hashes, temporary paths,
    numbers (line numbers, timings, counts) and whitespace."""
    text = _TEMP_PATH.sub("<tmp>", message or "")
    text = _SHA.sub("<sha>", text)
    text = _NUMBER.sub("N", text)
    return _SPACE.sub(" ", text).strip()[:MESSAGE_LIMIT]


def finding_key(finding: Mapping[str, Any]) -> tuple[str, str, str, str]:
    """(kind, check, location, normalized message) of one finding."""
    location = finding.get("module") or finding.get("file") or finding.get("path") or ""
    return (
        str(finding.get("kind") or ""),
        str(finding.get("check") or finding.get("linter") or ""),
        str(location),
        normalize(str(finding.get("message") or "")),
    )


def signature(tip: str, findings: Iterable[Mapping[str, Any]]) -> str:
    """The failure signature of a checkpoint: its tip and its normalized findings, in any order."""
    keys = sorted({finding_key(f) for f in findings if isinstance(f, Mapping)})
    payload = json.dumps([tip, keys], separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()[:24]


def notes_of(worker_notes: str | None) -> dict[str, Any]:
    try:
        notes = json.loads(worker_notes or "{}")
    except ValueError:
        return {}
    return notes if isinstance(notes, dict) else {}


def checkpoint_signature(tip: str, worker_notes: str | None) -> str | None:
    """The signature of a stored checkpoint submission, or None when it carries no findings."""
    findings = notes_of(worker_notes).get("repair_checkpoint")
    if not isinstance(findings, list) or not findings:
        return None
    return signature(tip, findings)


def revision(value: Any) -> str:
    """A worker revision as the claim endpoint accepts it (a hex commit id), else empty."""
    return value if isinstance(value, str) and re.fullmatch(r"[0-9a-f]{7,64}", value) else ""


def conclusion(text: str | None, limit: int = CONCLUSION_LIMIT) -> str:
    """The agent's own conclusion from its last message: the paragraphs that say why it could not make
    progress (a human has to intervene, no source change can fix it), else the message's opening, cut
    to `limit` characters. Empty when the agent said nothing."""
    if not isinstance(text, str) or not text.strip():
        return ""
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    chosen = [p for p in paragraphs if _NEEDS_HUMAN.search(p)] or paragraphs[:1]
    result = _SPACE.sub(" ", " ".join(chosen)).strip()
    return result if len(result) <= limit else result[: limit - 1].rstrip() + "…"


def retry_delay(retries: int, first: int, most: int) -> int:
    """The wait before the next claim after `retries` earlier backoffs of this stall."""
    first = max(60, int(first))
    return int(min(first * 2 ** min(20, max(0, retries)), max(first, int(most))))


def clock(at: float) -> str:
    """An epoch time as AEDT (or AEST) wall-clock text, for alerts and messages."""
    return datetime.fromtimestamp(at, TIMEZONE).strftime("%a %d %b %H:%M %Z")


def duration(seconds: float) -> str:
    minutes = int(max(0, seconds) // 60)
    return f"{minutes // 60}h {minutes % 60:02d}m" if minutes >= 60 else f"{minutes}m"


def describe_findings(findings: Iterable[Mapping[str, Any]], limit: int = 3) -> list[str]:
    """Up to `limit` findings as short lines: kind, location and message."""
    lines = []
    for finding in findings:
        if not isinstance(finding, Mapping):
            continue
        kind, check, location, _ = finding_key(finding)
        label = "/".join(x for x in (kind, check) if x) or "finding"
        where = f" {location}" if location else ""
        message = _SPACE.sub(" ", str(finding.get("message") or "")).strip()[:240]
        lines.append(f"{label}{where}: {message}")
        if len(lines) == limit:
            break
    return lines

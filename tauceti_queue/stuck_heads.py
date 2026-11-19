"""Open PR heads stuck without CI, as the hourly CI recovery pass on this host reports them.

TauCetiTools' `ci_recovery.py` (`tauceti-ci-recovery.service`) already lists every open PR head
each hour. It writes the heads that, three hours after their last push, have neither CI nor a final
state to a small JSON report (`ci_recovery_report` in the configuration):

    {"schema": "tauceti.ci-recovery.stuck-heads/v1", "repo": "OWNER/NAME", "observed_at": 1795000000.0,
     "complete": true, "stuck_since": 1794990000.0,
     "heads": [{"pr": 123, "head": "<sha>", "reason": "no-ci", "last_push_at": 1794980000.0, "run": null}]}

`reason` is `no-ci` (no `build` status and no live pr-build) or `retry-interrupted` (the build was
cancelled at the job timeout or mid-build, and so was its one automatic retry). `stuck_since` is
the first pass of the current run of passes that found any stuck head.

The queue reads the report for its status (`stuck_heads`, and the summary's "stuck heads: N, oldest
Xh" line) and, once the count has stayed above zero for `PERSIST_SECONDS`, sends the operator one
direct message per stuck period through the escalation notifier (`escalation.md`).
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .escalation import quote

SCHEMA = "tauceti.ci-recovery.stuck-heads/v1"
# The recovery pass runs hourly; a report older than three passes no longer describes the heads.
STALE_SECONDS = 3 * 3600
# How long the count must stay above zero before the operator is messaged.
PERSIST_SECONDS = 6 * 3600
# The most heads the status document and a message list.
LISTED = 30
MAX_BYTES = 1_000_000
# The status alert's wording, which the public observation recognises (`monitoring.py`).
ALERT_TEXT = "stuck without CI or a final state"
TIMEZONE = ZoneInfo("Australia/Sydney")
# A report from the future beyond this clock skew is not trusted.
SKEW_SECONDS = 600
# Epoch seconds beyond this are not times (year 5138).
LATEST = 1e11


def _number(value: Any) -> float | None:
    """An epoch time as a float, or None unless it is a finite, representable, nonnegative number."""
    if type(value) not in (int, float) or not 0 <= value < LATEST:
        return None
    return float(value) if math.isfinite(value) else None


def read(path: str | Path | None, now: float, repository: str | None = None) -> dict[str, Any]:
    """The report as a status section: `state` is `fresh`, `stale`, `missing`, `invalid` or `unconfigured`."""
    if not path:
        return {"state": "unconfigured"}
    section: dict[str, Any] = {"state": "missing", "path": str(path)}
    try:
        with Path(path).open("rb") as stream:
            data = stream.read(MAX_BYTES + 1)
    except FileNotFoundError:
        return section
    except OSError as error:
        return section | {"state": "invalid", "detail": f"{type(error).__name__}: {error}"[:300]}
    try:
        if len(data) > MAX_BYTES:
            raise ValueError("report exceeds limit")
        report = json.loads(data)
        if not isinstance(report, Mapping) or report.get("schema") != SCHEMA:
            raise ValueError("unknown report schema")
        if repository and report.get("repo") != repository:
            raise ValueError(f"report is for {report.get('repo')!r}")
        observed = _number(report.get("observed_at"))
        if observed is None or observed > now + SKEW_SECONDS:
            raise ValueError("report has no valid observation time")
        if not isinstance(report.get("heads"), list):
            raise ValueError("report has no list of heads")
        heads = []
        for head in report["heads"]:
            pushed = _number(head.get("last_push_at")) if isinstance(head, Mapping) else None
            if type(head.get("pr")) is not int or not 0 < head["pr"] < 10**9 or pushed is None or pushed > observed:
                raise ValueError("malformed stuck head")
            heads.append(
                {
                    "pr": head["pr"],
                    "head": str(head.get("head") or "")[:40],
                    "reason": str(head.get("reason") or "")[:40],
                    "last_push_at": pushed,
                    "run": head.get("run") if type(head.get("run")) is int else None,
                }
            )
        since = _number(report.get("stuck_since")) if heads else None
        if heads and (since is None or since > observed):
            raise ValueError("stuck heads without a valid stuck_since")
    except (ValueError, TypeError, AttributeError, OverflowError, RecursionError) as error:
        return section | {"state": "invalid", "detail": f"{type(error).__name__}: {error}"[:300]}
    heads.sort(key=lambda h: (h["last_push_at"], h["pr"]))
    return section | {
        "state": "stale" if now - observed > STALE_SECONDS else "fresh",
        "repo": report.get("repo"),
        "observed_at": observed,
        "complete": report.get("complete") is True,
        "count": len(heads),
        "oldest_seconds": max(0.0, now - heads[0]["last_push_at"]) if heads else None,
        "stuck_since": since,
        "heads": heads[:LISTED],
    }


def _count(section: Mapping[str, Any]) -> str:
    """The count, as a lower bound when the pass stopped before classifying every head."""
    return f"{section['count']}" if section.get("complete") else f"at least {section['count']} (partial pass)"


def _hours(seconds: float | None) -> str:
    return f"{int((seconds or 0) // 3600)}h"


def summary(section: Mapping[str, Any] | None, now: float | None = None) -> str | None:
    """The status summary's line, or None when no report is configured."""
    if not section or section.get("state") == "unconfigured":
        return None
    state = section.get("state")
    if state in ("missing", "invalid"):
        return f"stuck heads: report {state}" + (f" ({section['detail']})" if section.get("detail") else "")
    line = f"stuck heads: {section.get('count', 0)}"
    if section.get("count"):
        line += f", oldest {_hours(section.get('oldest_seconds'))}"
    if state == "stale" and now is not None and section.get("observed_at") is not None:
        line += f" (report stale, {_hours(now - section['observed_at'])} old)"
    elif not section.get("complete"):
        line += " (partial pass)"
    return line


def alert(section: Mapping[str, Any], now: float) -> str | None:
    """The status alert once the stuck count has stayed above zero for `PERSIST_SECONDS`."""
    since = section.get("stuck_since")
    if section.get("state") != "fresh" or not section.get("count") or since is None or now - since < PERSIST_SECONDS:
        return None
    return (
        f"{_count(section)} PR heads {ALERT_TEXT} for {_hours(now - since)} "
        f"(oldest {_hours(section.get('oldest_seconds'))})"
    )


def notice(section: Mapping[str, Any], now: float, public_url: str = "") -> tuple[str, str] | None:
    """The (outbox key, message) of the one direct message for the current stuck period, once due."""
    if alert(section, now) is None:
        return None
    repo = str(section.get("repo") or "")
    since = int(section["stuck_since"])
    started = datetime.fromtimestamp(since, TIMEZONE).strftime("%a %d %b %H:%M %Z")
    lines = [
        f"**Tau Ceti CI: {_count(section)} PR heads {ALERT_TEXT} since {started}**",
        "",
        "Each has had no `build` status and no live pr-build three hours after its last push, or its build "
        "and its one automatic retry were both interrupted. ci-recovery found them; nothing will re-run them.",
        "",
    ]
    for head in section.get("heads") or []:
        pushed = datetime.fromtimestamp(head["last_push_at"], TIMEZONE).strftime("%d %b %H:%M")
        lines.append(
            f"- https://github.com/{quote(repo, 100)}/pull/{head['pr']} {quote(head['reason'], 40)}, "
            f"pushed {pushed} ({_hours(now - head['last_push_at'])} ago)"
        )
    if section["count"] > len(section.get("heads") or []):
        lines.append(f"- and {section['count'] - len(section['heads'])} more")
    if public_url:
        lines += ["", f"Status: {public_url.rstrip('/')}/status"]
    return f"stuck-heads:{repo}:{since}", "\n".join(lines)

"""The queue head's live build progress, read from the public CI operations snapshot.

TauCetiTools' trusted build reporter (`scripts/ci_live_progress.py`, outside the bwrap sandbox)
posts bounded counters every fifteen seconds: the sandbox phase, Lake's own `[done/total]` job
counter and its rate over the last ten minutes. TauCetiCI publishes them on the run's job in the
public jobs pages. `tauceti-queue status` reads them here with public, unauthenticated requests and
no GitHub calls. The counters come from candidate output, so they are observations only and never
inform a queue decision. Any failure or missing field leaves the head showing its running step alone.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

ORIGIN = "https://ci-data.taucetiproject.org"
MANIFEST = ORIGIN + "/v1/operations/manifest.json"
REPORTED = "untrusted_counters_trusted_reporter"
DISPLAY_TIMEZONE = "Australia/Sydney"
MAX_JOB_PAGES = 5
MAX_COUNT = 10_000_000
PHASES = {
    "cache-unpack": "cache unpack",
    "build": "build",
    "audit-setup": "audit setup",
    "audit-import": "audit import",
    "audit-duplicates": "duplicate audit",
    "audit-axioms": "axiom audit",
    "audit-module-system": "module system",
    "lint-env": "environment lint",
    "lint-style": "style lint",
    "declaration-names": "record declaration names",
    "stage-cache": "stage cache",
    "end": "finished",
}


def _instant(value: Any) -> float:
    if not isinstance(value, str) or len(value) > 40:
        raise ValueError("missing observation time")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("observation time has no timezone")
    return parsed.timestamp()


def _count(value: Any) -> int | None:
    return value if type(value) is int and 0 <= value <= MAX_COUNT else None


def _object_url(scope: str, reference: Any) -> str:
    path = reference.get("url") if isinstance(reference, Mapping) else None
    if not isinstance(path, str) or not re.fullmatch(r"/v1/operations/objects/" + scope + r"/[a-f0-9]{64}\.json", path):
        raise ValueError("invalid public object URL")
    return ORIGIN + path


def reported(value: Any) -> dict[str, Any] | None:
    """The bounded fields of one job's live report, or None for a job without a reporter."""
    if not isinstance(value, Mapping) or value.get("coverage") != REPORTED:
        return None
    _instant(value.get("observed_at"))
    phase = value.get("phase")
    result: dict[str, Any] = {
        "observed_at": value["observed_at"],
        "phase": phase if phase in PHASES else None,
        "completed_tasks": None,
        "total_tasks": None,
        "tasks_per_minute": None,
        "completed_modules": _count(value.get("completed_modules")),
    }
    done, total = _count(value.get("completed_tasks")), _count(value.get("total_tasks"))
    if done is not None and total and done <= total:
        result.update(completed_tasks=done, total_tasks=total)
    rate = value.get("tasks_per_minute")
    if type(rate) in (int, float) and 0 <= rate <= MAX_COUNT:
        result["tasks_per_minute"] = float(rate)
    return result


def lookup(run_id: Any, run_attempt: Any, fetch: Callable[..., dict[str, Any]]) -> dict[str, Any] | None:
    """The latest live report for a run attempt; active jobs lead the public jobs pages."""
    manifest = fetch(MANIFEST, timeout=3)
    scope = manifest.get("scope") if isinstance(manifest, Mapping) else None
    if (
        manifest.get("schema") != "tauceti.operations.manifest/v1"
        or not isinstance(scope, str)
        or not re.fullmatch(r"[a-f0-9]{64}", scope)
    ):
        raise ValueError("invalid public manifest")
    root = fetch(_object_url(scope, (manifest.get("domains") or {}).get("jobs")), timeout=3)
    found = []
    for reference in (root.get("job_pages") or [])[:MAX_JOB_PAGES]:
        records = fetch(_object_url(scope, reference), timeout=3).get("records") or []
        for job in records:
            if (
                isinstance(job, Mapping)
                and str(job.get("run_id")) == str(run_id)
                and (job.get("run_attempt") or 1) == (run_attempt or 1)
            ):
                progress = reported(job.get("progress"))
                if progress:
                    found.append(progress)
        if found or not any(isinstance(job, Mapping) and job.get("active") for job in records):
            break
    return max(found, key=lambda p: _instant(p["observed_at"])) if found else None


def attach(document: dict[str, Any], fetch: Callable[..., dict[str, Any]]) -> None:
    """Add `head.ci.build_progress` while the head's CI run is in progress; never raises."""
    try:
        ci = (document.get("head") or {}).get("ci") or {}
        if ci.get("status") != "in_progress" or not ci.get("run_id"):
            return
        progress = lookup(ci["run_id"], ci.get("run_attempt"), fetch)
        if progress:
            ci["build_progress"] = progress
    except (OSError, RuntimeError, ValueError, TypeError, KeyError, AttributeError):
        return


def text(progress: Mapping[str, Any], now: float, timezone: str = DISPLAY_TIMEZONE) -> str | None:
    """`build: phase build, 113,917/113,955 Lake jobs (99.9%), 850/min, ETA 19 Nov 15:40 AEDT`."""
    try:
        parts = []
        phase = progress.get("phase")
        if phase in PHASES:
            parts.append("phase " + PHASES[phase])
        done, total = _count(progress.get("completed_tasks")), _count(progress.get("total_tasks"))
        modules = _count(progress.get("completed_modules"))
        if done is not None and total and done <= total:
            percent = min(100 * done / total, 99.9) if done < total else 100.0
            parts.append(f"{done:,}/{total:,} Lake jobs ({percent:.1f}%)")
            rate = progress.get("tasks_per_minute")
            if type(rate) in (int, float) and rate >= 0 and phase == "build" and done < total:
                parts.append(f"{rate:,.0f}/min")
                if rate > 0:
                    eta = _instant(progress["observed_at"]) + (total - done) * 60 / rate
                    parts.append("ETA " + datetime.fromtimestamp(eta, ZoneInfo(timezone)).strftime("%d %b %H:%M %Z"))
        elif modules and phase == "build":
            # Reporters before Lake's job counter count only built TauCeti modules, with no total.
            parts.append(f"{modules:,} TauCeti modules built")
        if not parts:
            return None
        age = now - _instant(progress["observed_at"])
        if age > 120:
            parts.append(f"reported {int(age) // 60}m ago")
        return "build: " + ", ".join(parts)
    except (ValueError, TypeError, KeyError, OverflowError):
        return None

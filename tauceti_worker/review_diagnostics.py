"""Persistent local diagnostics and allow-listed public summaries for failed review commands."""

from __future__ import annotations

import datetime
import json
import re
from pathlib import Path

from .runtime_status import atomic_json

_ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
_SECRET_RE = [
    re.compile(
        r"\b(?:sk-[A-Za-z0-9_-]{8,}|gh[oprsu]_[A-Za-z0-9]{8,}|"
        r"github_pat_[A-Za-z0-9_]{8,}|xoxb-[A-Za-z0-9-]{8,})\b"
    ),
    re.compile(r"(?i)\b(?:authorization|bearer)\s*[:=]?\s+\S+"),
    re.compile(r"(?i)\b[A-Z][A-Z0-9_]*(?:KEY|TOKEN|SECRET|PASSWORD)[A-Z0-9_]*=\S+"),
    re.compile(r"https://[^/\s@]+@"),
]
_HOME_RE = re.compile(r"(?:(?:/home|/Users)/)[^/\s]+")
_PUBLIC_CATEGORIES = {
    "reviewer-auth",
    "missing-tool",
    "stale-head",
    "provider-unavailable",
    "checkout-or-network",
    "review-engine",
    "review-command",
}
_PUBLIC_PROVIDERS = {"claude", "codex", "deepseek", "kiro", "minimax", "sonnet"}
_PUBLIC_DETAILS = {
    "reviewer-auth": "reviewer authentication failed",
    "missing-tool": "reviewer executable unavailable",
    "stale-head": "PR head changed before review",
    "provider-unavailable": "review provider unavailable",
    "checkout-or-network": "checkout or network operation failed",
    "review-engine": "review engine failed",
    "review-command": "review command failed",
}
_STAMP_RE = re.compile(r"\A\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z\Z")
# Fixed, publishable explanations. Never copy arbitrary provider output into an issue.
_FAILURE_DETAILS = (
    (
        "argument-limit",
        "review-engine",
        "review prompt exceeded the OS argument limit",
        re.compile(r"argument list too long|\bE2BIG\b", re.I),
    ),
    (
        "github-rate-limit",
        "checkout-or-network",
        "GitHub API rate limit prevented review",
        re.compile(r"(?:^|:\s)gh:.*rate limit|API rate limit exceeded", re.I),
    ),
    (
        "github-permission",
        "checkout-or-network",
        "GitHub rejected an operation for lack of permission",
        re.compile(
            r"Resource not accessible by integration|Resource not accessible by personal access token"
            r"|refusing to allow a GitHub App|gh:.*\bHTTP 403\b",
            re.I,
        ),
    ),
    (
        "model-unavailable",
        "provider-unavailable",
        "the selected reviewer model is unavailable",
        re.compile(r"model.{0,120}(?:not supported|not available|do not have access)|unknown model", re.I),
    ),
    (
        "disk-full",
        "review-engine",
        "the worker ran out of disk space",
        re.compile(r"No space left on device|disk quota exceeded", re.I),
    ),
    (
        "timeout",
        "checkout-or-network",
        "a review operation timed out",
        re.compile(r"TimeoutExpired|Request timed out|deadline exceeded", re.I),
    ),
)
_LOG_TAIL_BYTES = 128 * 1024


def sanitize_failure(text: str, limit: int = 500) -> str:
    """Return a concise one-line diagnostic for local retention only.

    This best-effort cleanup makes local state easier to inspect, but it is deliberately not a
    publication boundary: arbitrary subprocess output can contain unanticipated credential forms.
    `public_review_failure` publishes only fixed allow-listed facts and never this returned text.
    """
    clean = _ANSI_RE.sub("", str(text or ""))
    clean = _CONTROL_RE.sub(" ", clean)
    for pattern in _SECRET_RE:
        clean = pattern.sub("[REDACTED]", clean)
    clean = _HOME_RE.sub("/[home]", clean)
    clean = " ".join(clean.split())
    return clean[-limit:]


def classify_failure(summary: str) -> str:
    """Coarse failure class used for alerts and future retry policy."""
    low = summary.lower()
    for _, category, _, pattern in _FAILURE_DETAILS:
        if pattern.search(summary):
            return category
    if re.search(r"(?:review-root lookup failed:|gh api .+ FAILED:).*HTTP \d{3}", summary):
        return "checkout-or-network"
    if any(s in low for s in ("not logged in", "run /login", "authentication", "credential")):
        return "reviewer-auth"
    if any(s in low for s in ("not found on path", "no such file or directory", "command not found")):
        return "missing-tool"
    if "expected" in low and "head" in low:
        return "stale-head"
    # "subscription window is exhausted" is how the engine's provider-down abort names an exhausted
    # plan, and the subscription CLIs' own wording ("You've hit your session limit · resets 9:30pm")
    # carries no status code at all, so neither matches the rate-limit vocabulary above. Both are the
    # provider declining to serve, which is what this category means and what it publishes.
    if any(
        s in low
        for s in (
            "rate limit",
            "too many requests",
            "429",
            "overloaded",
            "529",
            "subscription window",
            "hit your session limit",
            "hit your weekly limit",
            "hit your monthly",
            "usage limit",
            "spend limit",
        )
    ):
        return "provider-unavailable"
    if any(s in low for s in ("git clone", "git fetch", "could not resolve host", "connection reset")):
        return "checkout-or-network"
    if any(s in low for s in ("traceback", "exception", "error:")):
        return "review-engine"
    return "review-command"


def failure_summary(log_file: Path | None, reason: str = "") -> str:
    """Retain the operative error even when a wrapper/cleanup line follows it.

    Read a bounded tail, rank specific diagnostics above generic engine errors, and
    break ties in favour of the latest line. This only diagnoses; it never refunds
    or resets the review-error budget. Raw output remains local.
    """
    lines = []
    if log_file is not None:
        try:
            with log_file.open("rb") as stream:
                size = stream.seek(0, 2)
                stream.seek(max(0, size - _LOG_TAIL_BYTES))
                if size > _LOG_TAIL_BYTES:
                    stream.readline()  # discard the partial first line
                lines = stream.read(_LOG_TAIL_BYTES).decode("utf-8", "replace").splitlines()
        except OSError:
            pass
    # Command echoes delimit subprocess phases; they are not failures. The CLI
    # also prints model-written scoreboard/thread bodies between these separators.
    # Never classify that prose, or a recoverable per-rubric stderr diagnostic, as
    # the cause of a later command failure.
    candidates = []
    in_review_text = False
    for raw in lines:
        line = _ANSI_RE.sub("", raw).strip()
        # stdout (review prose) can flush around stderr (the command and its
        # error). A new subprocess echo ends the prose phase, and die() is the
        # parent's final stderr write: anything later is buffered review text.
        if line.startswith("tauceti-review:"):
            candidates.append(line)
            break
        if line.startswith("$ ") or line.startswith("=== running review"):
            candidates.clear()
            in_review_text = False
            continue
        if line == "=" * 72:
            candidates.clear()
            in_review_text = not in_review_text
            continue
        if in_review_text:
            continue
        if re.match(r"^\[[^]]+\]", line):
            continue
        if line:
            candidates.append(line)
    if reason.strip():
        candidates.append(_ANSI_RE.sub("", reason).strip())

    def rank(line):
        if any(p.search(line) for _, _, _, p in _FAILURE_DETAILS):
            return 3
        category = classify_failure(line)
        if category not in ("review-command", "review-engine"):
            # A command name alone (e.g. a clone progress message) is not evidence
            # that the operation failed.
            if category == "checkout-or-network" and not re.search(
                r"error|fatal:|failed|could not resolve host|connection reset", line, re.I
            ):
                return 0
            return 2
        if category == "review-engine" or re.search(r"\b\w+(?:Error|Exception):|^fatal:", line):
            return 1
        return 0

    # Strip ANSI before classifying, but truncate only AFTER selecting the diagnostic.
    if not candidates:
        return ""
    _, chosen = max(enumerate(candidates), key=lambda pair: (rank(pair[1]), pair[0]))
    summary = sanitize_failure(chosen)
    for _, _, _, pattern in _FAILURE_DETAILS:
        match = pattern.search(chosen)
        if match and not pattern.search(summary):
            # A long filename/command after the error must not truncate away the
            # very signature that made this line useful.
            return sanitize_failure(chosen[: match.end()])
    return summary


def _path(state: Path, pr: int) -> Path:
    return state / f"review-failure-{pr}.json"


def read_review_failure(state: Path, pr: int) -> dict:
    try:
        value = json.loads(_path(state, pr).read_text())
    except (OSError, ValueError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


def recover_review_failures(state: Path, log_dir: Path, *, worker: str, pr: int, head: str = "") -> dict:
    """Backfill diagnostics from review logs written before structured retention was deployed."""
    existing = read_review_failure(state, pr)
    if existing:
        return existing
    try:
        logs = sorted(log_dir.glob(f"review-{pr}-*.log"), key=lambda path: path.stat().st_mtime)[-3:]
    except OSError:
        return {}
    attempts = []
    for log_file in logs:
        summary = failure_summary(log_file)
        if not summary:
            continue
        try:
            stamp = datetime.datetime.fromtimestamp(log_file.stat().st_mtime, datetime.UTC)
        except OSError:
            stamp = datetime.datetime.now(datetime.UTC)
        attempts.append(
            {
                "at": stamp.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "worker": worker,
                "head": head,
                "provider": "unknown",
                "code": None,
                "category": classify_failure(summary),
                "summary": summary,
                "log": log_file.name,
                "recovered": True,
            }
        )
    if not attempts:
        return {}
    value = {"schema": "tauceti.review-failure/v1", "pr": pr, "attempts": attempts}
    atomic_json(_path(state, pr), value)
    return value


def record_review_failure(
    state: Path,
    *,
    worker: str,
    pr: int,
    head: str,
    provider: str,
    code: int,
    reason: str = "",
    log_file: Path | None = None,
) -> dict:
    """Append one failure, retaining the latest three attempts for this PR and worker."""
    summary = failure_summary(log_file, reason)
    if not summary:
        summary = f"review command exited with status {code}"
    attempt = {
        "at": datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "worker": worker,
        "head": head,
        "provider": provider,
        "code": int(code),
        "category": classify_failure(summary),
        "summary": summary,
        "log": log_file.name if log_file is not None else None,
    }
    previous = read_review_failure(state, pr)
    attempts = previous.get("attempts") if isinstance(previous.get("attempts"), list) else []
    value = {"schema": "tauceti.review-failure/v1", "pr": pr, "attempts": [*attempts, attempt][-3:]}
    atomic_json(_path(state, pr), value)
    return value


def clear_review_failure(state: Path, pr: int) -> None:
    _path(state, pr).unlink(missing_ok=True)


def public_review_failure(value: dict) -> str:
    """Compact public account built only from fixed labels; never publish subprocess text."""
    attempts = value.get("attempts") if isinstance(value.get("attempts"), list) else []
    rows = []
    for attempt in attempts[-3:]:
        if not isinstance(attempt, dict):
            continue
        category = attempt.get("category")
        category = category if category in _PUBLIC_CATEGORIES else "review-command"
        provider = attempt.get("provider")
        provider = provider if provider in _PUBLIC_PROVIDERS else "unknown"
        stamp = attempt.get("at")
        stamp = stamp if isinstance(stamp, str) and _STAMP_RE.fullmatch(stamp) else "unknown time"
        code = attempt.get("code")
        exit_note = f"exit {code}" if isinstance(code, int) else "exit unknown"
        summary = str(attempt.get("summary", "")).lower()
        detail = next(
            (detail for _, _, detail, pattern in _FAILURE_DETAILS if pattern.search(summary)), _PUBLIC_DETAILS[category]
        )
        rows.append(f"- {stamp}: `{category}` via `{provider}` ({exit_note}): {detail}")
    return "\n".join(rows)


def public_diagnostic_quality(body: str) -> int:
    """Permit a strictly better public diagnosis, never peer-to-peer churn.

    Only recognize complete fixed diagnostic rows, not arbitrary prose elsewhere
    in the issue. Missing < generic command failure < engine error < specific cause.
    """
    quality = 0
    for category, detail in re.findall(
        r"^- .*: `([a-z-]+)` via `[a-z]+` \(exit (?:-?\d+|unknown)\): (.+)$", body, re.M
    ):
        if detail in {d for _, _, d, _ in _FAILURE_DETAILS}:
            quality = max(quality, 3)
        elif category in _PUBLIC_DETAILS and detail == _PUBLIC_DETAILS[category]:
            quality = max(quality, 1 if category == "review-command" else 2 if category == "review-engine" else 3)
    return quality

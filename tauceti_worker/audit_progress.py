"""Trusted coverage checks and continuation instructions for one audit session."""

from __future__ import annotations

import json
import re
from pathlib import Path

from .audit_reader import pages
from .config import Die

GOAL_OBJECTIVE = (
    "Inspect the entire immutable audit bundle (every page when paged) and every commit for IO and "
    "executable metaprogramming beyond macros/notation. Preserve all findings and "
    "unresolved code questions across compaction. Finish with one safety judgment "
    "for the entire batch; paged input requires complete reader coverage and its final token."
)
GOAL_REQUEST = (
    "\nThe user explicitly requests a persistent goal (the /goal feature). Call get_goal; "
    "if no unfinished goal exists, call create_goal with this exact objective, without a token budget: "
    + GOAL_OBJECTIVE
    + "\nKeep working toward this goal while pages remain. Goal status is bookkeeping, "
    "never evidence of source safety. Only audit.read_page (when paged) and get_goal/create_goal/update_goal "
    "are available. Never discard a finding to complete the goal.\n"
)
READING_POLICY = """
Complete the inspection; unread pages are remaining work, not a code-safety question.
The reader reports next_unread_page and remaining_pages. Read the next unread page,
and preserve progress, all findings and unresolved code questions across compaction.
If this turn ends before you finish and there is no source finding or unresolved
code-safety question, return verdict "unfinished" with findings []. This is progress,
not approval. Use "incomplete" with specific findings for actual code uncertainty;
use "finding" for a source concern, even if other pages remain unread. Never hide
a source concern in an unfinished response. Return pass only after all pages have
been inspected. The Worker independently verifies coverage and can continue this
same conversation after an unfinished turn. Never start independent partial audits.
"""
INLINE_CONTINUE_PROMPT = (
    "The trusted Worker has not received a complete safety judgment. Continue this SAME retained "
    "inline inspection and goal, preserving all findings and code uncertainty. The complete bundle "
    "remains in this conversation; inspect it and finish the judgment. Never infer safety from this request.\n"
)
INLINE_POLICY = (
    "\nThe ENTIRE bundle is included inline below. There are no pages and no read_page tool. "
    "Inspect the supplied JSON directly. Finish with pass, finding or incomplete; "
    "unfinished is only for paged input.\n"
)


def coverage(batch: dict, receipt: Path) -> dict:
    """Validate the owned receipt; absence means no source has been delivered yet."""
    count = len(pages(batch))
    if not receipt.exists():
        return {"pages": count, "read": [], "missing": list(range(count)), "last_token": None}
    value = json.loads(receipt.read_text())
    if not isinstance(value, dict):
        raise Die("security-audit: invalid retained coverage receipt")
    seen = value.get("read")
    if (
        set(value) - {"input_digest", "pages", "read", "last_token", "last_page"}
        or value.get("input_digest") != batch["input_digest"]
        or type(value.get("pages")) is not int
        or value["pages"] != count
        or not isinstance(seen, list)
        or any(type(i) is not int or not 0 <= i < count for i in seen)
        or sorted(set(seen)) != seen
        or not isinstance(value.get("last_token"), str)
        or (seen and not value["last_token"])
        or (
            value.get("last_page") is not None
            and (type(value["last_page"]) is not int or value["last_page"] not in seen)
        )
    ):
        raise Die("security-audit: invalid retained coverage receipt")
    return {**value, "missing": [i for i in range(count) if i not in seen]}


def legacy_coverage_only(value, batch: dict, receipt: Path) -> bool:
    """Version-2 compatibility: prove a coverage diagnostic from the actual gaps.

    This recognizes a diagnostic grammar, never a commit, verdict waiver, or a
    claim about inspected code. Unknown prose and additional findings stay held.
    """
    if not isinstance(value, dict) or set(value) != {"verdict", "findings", "read_token"}:
        return False
    if (
        value.get("verdict") != "incomplete"
        or not isinstance(value.get("findings"), list)
        or len(value["findings"]) != 1
        or not isinstance(value["findings"][0], str)
    ):
        return False
    match = re.fullmatch(
        r"Commit ([1-9][0-9]*): inspection is incomplete\. Pages ([0-9]+)[–-]([0-9]+) "
        r"have not been reviewed, so the safety of all changed source files cannot be established\.",
        value["findings"][0],
    )
    if not match or not 1 <= int(match[1]) <= len(batch["commits"]):
        return False
    proof = coverage(batch, receipt)
    return (
        bool(proof["read"])
        and proof["missing"] == list(range(int(match[2]), int(match[3]) + 1))
        and value["read_token"] == proof["last_token"]
    )


def continuation_prompt(batch: dict, receipt: Path) -> str:
    proof = coverage(batch, receipt)
    remaining = proof["missing"]
    if remaining:
        status = f"{len(remaining)} pages remain unread; next_unread_page={remaining[0]}."
    else:
        status = "All pages were delivered; finish the safety judgment, retaining every source concern."
    return (
        "The trusted Worker has not accepted a complete safety judgment. " + status + " "
        "Continue this SAME retained inspection and goal, preserving all findings and code uncertainty. "
        "Use an empty reader token to replay the last full page if the token was lost. "
        "Read next_unread_page from the returned progress and continue until finished. "
        "Unread source is work to do; never infer safety from coverage, goal completion, or this request.\n"
    )


def snapshot(batch: dict, receipt: Path, work: Path) -> str:
    proof = coverage(batch, receipt)
    value = {
        "input_digest": batch["input_digest"],
        "pages": proof["pages"],
        "read": proof["read"],
        "last_token": proof["last_token"] or "",
    }
    if "last_page" in proof:
        value["last_page"] = proof["last_page"]
    path = work / "coverage-before.json"
    path.write_text(json.dumps(value))
    return continuation_prompt(batch, path)


def continuable(
    transcript: str, batch: dict, receipt: Path, work: Path, *, legacy=False, allowed=(), paged=True
) -> bool:
    from . import security_audit as sa
    from .audit_recovery import native_finals, safe_history

    try:
        if not safe_history(work, batch, paged, legacy=legacy, allowed=allowed):
            return False
        events = sa.codex_events(transcript, paged)
        thread = sa.retained_thread(work)
        if any(e.get("type") == "thread.started" and e.get("thread_id") != thread for e in events):
            return False
        proof = coverage(batch, receipt) if paged else None
        messages = [
            e for e in events if e.get("type") == "item.completed" and e.get("item", {}).get("type") == "agent_message"
        ]
        if not messages:
            return any(e.get("type") == "turn.completed" for e in events)
        if not paged:
            return False
        value = sa.codex_final(transcript, True)
        finals = native_finals(work)
        if not finals or finals[-1] != value:
            return False
        if (
            not isinstance(value, dict)
            or set(value) != {"verdict", "findings", "read_token"}
            or value["findings"] != []
        ):
            return False
        token = value["read_token"]
        if not isinstance(token, str) or (proof["read"] and token != proof["last_token"]):
            return False
        return value["verdict"] == "unfinished" or (value["verdict"] == "pass" and bool(proof["missing"]))
    except (Die, OSError, ValueError, KeyError, TypeError, AttributeError):
        return False

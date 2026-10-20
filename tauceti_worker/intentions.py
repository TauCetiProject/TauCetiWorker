"""tauceti_worker.intentions — discover cross-contributor claims on the intentions board.

A claim is an open issue in the roadmap repo labelled `intention`, belonging to an area (see
in_area), that has been claimed (the intentions bot assigns the claimant). Roadmap workers avoid
targets claimed by *other* contributors; claims by the operator's own identity (or
operator-configured extra identities) are not avoided. A maintainer may additionally apply
`administrative-hold` to an assigned intention; every worker avoids that scope, including the
assignee's own workers, and failure to read administrative holds aborts authoring. Phase 1: the
avoid-list is the claim's prose scope, not matched against machine-readable target ids.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from .config import log, roadmap_extra_identities, roadmap_subareas
from .constants import ROADMAP
from .github import GitHub, me

# A claim's scope is untrusted text from an arbitrary issue body, injected into the worker prompt;
# bound its length and (in claimed_block) quote it so it reads as data, not instructions.
MAX_SCOPE_CHARS = 200
ADMINISTRATIVE_HOLD_LABEL = "administrative-hold"
# The area is matched client-side (in_area), so every open intention must be listed, not gh's 200.
INTENTION_LIMIT = 1000
_FIELDS = ["number", "url", "title", "body", "assignees", "labels"]


@dataclass
class Claim:
    number: int
    url: str
    holders: list[str]  # GitHub logins of the assignees (the claimants)
    scope: str  # prose description of what they're working on


def own_identities() -> set[str]:
    """The set of logins whose claims this worker will NOT avoid: its own `gh auth` identity plus
    any operator-configured extras. Lowercased for case-insensitive comparison."""
    return {me().lower()} | set(roadmap_extra_identities())


# GitHub issue forms render each field as a `### <label>` section; pull the "Items in scope" one.
_SCOPE_RE = re.compile(r"^###\s+Items in scope\s*$\n(.*?)(?=^###\s|\Z)", re.S | re.I | re.M)
_HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.S)
# GitHub issue forms render an unanswered field as "_No response_"; treat that as no scope.
_PLACEHOLDER_RE = re.compile(r"^_?no response_?$", re.I)


def parse_scope(body: str) -> str:
    """Extract the 'Items in scope' section of an Intention issue body, falling back to the whole
    body. HTML-comment markers are stripped and whitespace collapsed to a single line. An empty or
    `_No response_` placeholder answer yields ""; the caller then falls back to the issue title."""
    if not body:
        return ""
    m = _SCOPE_RE.search(body)
    text = m.group(1) if m else body
    text = _HTML_COMMENT_RE.sub("", text)
    text = re.sub(r"\s+", " ", text).strip()
    return "" if _PLACEHOLDER_RE.match(text) else text


def _label_names(issue: dict) -> set[str]:
    """Label names from `gh` JSON, accepting both its object form and simple string fixtures."""
    names = set()
    for label in issue.get("labels") or []:
        name = label.get("name", "") if isinstance(label, dict) else str(label)
        if name:
            names.add(name.lower())
    return names


_AREA_RE = re.compile(r"^###\s+Roadmap area\s*$\n(.*?)(?=^###\s|\Z)", re.S | re.I | re.M)
# Area names are CamelCase directory names, matched as whole, case-sensitive words so that prose
# such as "Fueter regularity" does not name the `Regularity` area.
_WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9]*")


def area_text(issue: dict) -> str:
    """Where an intention names its roadmap area: the issue form's "Roadmap area" answer, or the
    title and whole body when that section is missing or unanswered (bodies filed through the API)."""
    body = issue.get("body") or ""
    m = _AREA_RE.search(body)
    answer = re.sub(r"\s+", " ", _HTML_COMMENT_RE.sub("", m.group(1))).strip() if m else ""
    if answer and not _PLACEHOLDER_RE.match(answer):
        return answer
    return f"{issue.get('title') or ''}\n{body}"


def in_area(issue: dict, names: set[str]) -> bool:
    """Whether `issue` belongs to the area whose directory names (the area and its sub-roadmaps) are
    `names`: a `roadmap/<name>` label, or one of `names` as a word of area_text.

    Labels alone do not suffice: the issue form cannot turn its area dropdown into a label, and
    `gh issue list --label` on a label nobody applied returns no issues rather than an error."""
    labels = _label_names(issue)
    if any(f"roadmap/{n.lower()}" in labels for n in names):
        return True
    return not names.isdisjoint(_WORD_RE.findall(area_text(issue)))


def area_names(gh: GitHub, area: str) -> set[str]:
    """`area` and the sub-roadmaps nested in it."""
    return {area, *roadmap_subareas(gh, area)}


def select_foreign(issues: list[dict], own: set[str]) -> list[Claim]:
    """Pure filter: from raw intention-issue dicts, keep those claimed (assigned) by someone outside
    `own`. Unassigned issues (registered but unclaimed) and our-own claims are dropped. `own` must be
    lowercased. Kept separate from the network fetch so it's unit-testable with fixtures."""
    claims: list[Claim] = []
    for it in issues:
        if ADMINISTRATIVE_HOLD_LABEL in _label_names(it):
            continue  # binding holds are selected separately, including when held by our identity
        holders = [a.get("login", "") for a in (it.get("assignees") or []) if a.get("login")]
        if not holders:
            continue  # registered but unclaimed -> still up for grabs
        if any(h.lower() in own for h in holders):
            continue  # ours (or an operator-named identity)
        scope = parse_scope(it.get("body") or "") or (it.get("title") or "").strip()
        claims.append(Claim(number=int(it.get("number") or 0), url=it.get("url") or "", holders=holders, scope=scope))
    return claims


def select_administrative_holds(issues: list[dict]) -> list[Claim]:
    """Assigned administrative holds, regardless of who operates the current worker.

    An unassigned hold is inactive. This makes the intentions bot's normal expiry sweep release it
    by clearing the assignee, without requiring a second date parser in the worker.
    """
    holds: list[Claim] = []
    for it in issues:
        if ADMINISTRATIVE_HOLD_LABEL not in _label_names(it):
            continue
        holders = [a.get("login", "") for a in (it.get("assignees") or []) if a.get("login")]
        if not holders:
            continue
        scope = parse_scope(it.get("body") or "") or (it.get("title") or "").strip()
        holds.append(Claim(number=int(it.get("number") or 0), url=it.get("url") or "", holders=holders, scope=scope))
    return holds


def claimed_block(claims: list[Claim]) -> str:
    """Render foreign claims as the `__CLAIMED__` avoid-list for the prompt; "none" when empty.

    The scope is untrusted issue text, so it's truncated and JSON-quoted into a single token: the
    prompt frames these quoted strings as data (targets to avoid), never as instructions. Trusted
    metadata (issue number, claimant logins) sits outside the quoted scope."""
    if not claims:
        return "none"
    lines = []
    for c in claims:
        who = ", ".join(f"@{h}" for h in c.holders)
        scope = c.scope[:MAX_SCOPE_CHARS] or "(no description provided)"
        lines.append(f"- #{c.number} (claimed by {who}): {json.dumps(scope)}")
    return "\n".join(lines)


def avoid_list(gh: GitHub, area: str | None, respect: bool) -> str:
    """The `__CLAIMED__` string for a round: active administrative holds, then, when `respect`,
    scopes other contributors have claimed. A pinned `area` restricts both to it (see in_area); an
    unpinned round gets every hold and no claims, and its agent checks the board itself.

    One read serves both lists: every open intention when claims are wanted, else only the holds.
    It deliberately does not catch GitHub errors: authoring must stop if it cannot establish whether
    a maintainer hold is active. Picking the claims out of a successful read still fails open."""
    want_claims = bool(area) and respect
    labels = ["intention"] if want_claims else ["intention", ADMINISTRATIVE_HOLD_LABEL]
    issues = gh.issue_list(ROADMAP, labels=labels, fields=_FIELDS, limit=INTENTION_LIMIT)
    if area:
        names = area_names(gh, area)
        issues = [it for it in issues if in_area(it, names)]
    blocks = [claimed_block(select_administrative_holds(issues))]
    if want_claims:
        try:
            blocks.append(claimed_block(select_foreign(issues, own_identities())))
        except Exception as e:  # noqa: BLE001 - fail open on anything (soft, cooperative coordination)
            log(f"roadmap: could not resolve claims for {area} ({e}); proceeding without claim exclusions")
    return "\n".join(block for block in blocks if block != "none") or "none"

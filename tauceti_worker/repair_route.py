"""An operator can authorize one existing foreign-fork repair, never general authorship.

Unset by default. The exact PR/head/main scope expires on source movement; all
reads below are fresh and fail closed. It is not a request queue or permission
inferred from a label, a Git conflict, or maintainerCanModify alone.
"""

import json
import os
import re
from dataclasses import dataclass

from .constants import TAUCETI
from .github import GitHubError
from .github_transport import read_context
from .survey_facts import rebase_heads

ENV = "TAUCETI_REBASE_REPAIR_TARGET"
AUTHOR = "tauceti-ai-for-science"
FORK = AUTHOR + "/TauCeti"
PAUSE = {"keep", "hold", "wip", "human", "do-not-close"}


@dataclass(frozen=True)
class Target:
    pr: int
    head: str
    evidence_base: str  # immutable tested main in the original, head-deduplicated request
    base: str


def target() -> Target | None:
    match = re.fullmatch(r"([1-9][0-9]{0,9}):([a-f0-9]{40}):([a-f0-9]{40}):([a-f0-9]{40})", os.environ.get(ENV, ""))
    return Target(int(match[1]), match[2], match[3], match[4]) if match else None


def authorize(gh, pr: int, head: str, refname: str, head_repository: str) -> str:
    """Return the explicitly scoped main revision, or refuse without spending a task."""
    scope = target()
    if not scope or (scope.pr, scope.head) != (pr, head) or gh.repo != TAUCETI or head_repository != FORK:
        return ""
    try:
        with read_context(fresh=True, direct=True):
            live = json.loads(gh.api_jq(f"repos/{TAUCETI}/pulls/{pr}", ".") or "null")
            if not isinstance(live, dict):
                return ""
            source, base = live.get("head") or {}, live.get("base") or {}
            labels = {str(label.get("name", "")).lower() for label in live.get("labels", [])}
            if (
                live.get("state") != "open"
                or live.get("draft") is not False
                or live.get("maintainer_can_modify") is not True
                or live.get("user", {}).get("login") != AUTHOR
                or live.get("user", {}).get("type") != "User"
                or source.get("sha") != head
                or source.get("ref") != refname
                or source.get("repo", {}).get("full_name") != FORK
                or source.get("repo", {}).get("fork") is not True
                or base.get("ref") != "main"
                or base.get("repo", {}).get("full_name") != TAUCETI
                or "needs-rebase" not in labels
                or labels & PAUSE
            ):
                return ""
            if gh.api_jq(f"repos/{TAUCETI}", ".permissions.push") != "true":
                return ""
            if gh.api_jq(f"repos/{TAUCETI}/git/ref/heads/main", ".object.sha") != scope.base:
                return ""
            if scope.evidence_base != scope.base and gh.api_jq(
                f"repos/{TAUCETI}/compare/{scope.evidence_base}...{scope.base}", ".status"
            ) not in {"ahead", "identical"}:
                return ""
            # Bors intentionally publishes once per PR head, not once per main
            # promotion. Preserve that original evidence identity while the
            # operator separately names the one main snapshot to integrate.
            evidence_line = f"Tested main: `{scope.evidence_base}`. Checker policy: `local-source-v3`."
            for comment in gh.issue_comments(pr) or []:
                if (
                    isinstance(comment, dict)
                    and (comment.get("user") or {}).get("login") == "tau-ceti-bors[bot]"
                    and head in rebase_heads([comment])
                    and len(comment["body"]) <= 32768
                    and evidence_line in comment["body"].splitlines()
                ):
                    return scope.base
    except (GitHubError, ValueError, TypeError, AttributeError):
        pass
    return ""

"""The review App's `merge eligibility` checks: how TauCetiReview approves today.

TauCetiReview no longer comments `bors r+`; its reusable `merge-only.yml` and `merge-sweep.yml`
workflows run `runner/backend.py eligibility`, which publishes an App-owned check run named
`merge eligibility` on the reviewed head (TauCetiReview `runner/eligibility.py`). The check's
`external_id` is the decision, `{"schema": "tauceti-merge.eligibility/v1", "repo", "pr",
"merge_base_sha", "eligible", "review_safe", "single"}`, and its conclusion is `success` when
eligible, `failure` when the review is unsafe (approval withdrawn), `neutral` while waiting. The
checks describe eligibility independently of `MERGE_BACKEND`; whichever backend is selected admits.

Bors consumes them in `lib/worker/merge_eligibility.ex`: the trusted check with the highest id on
the live head decides; eligible admits the PR with the review App as reviewer (and `single` for Lake
pin changes) once the live merge base matches the reviewed one and no keep label is set; review
unsafe cancels; neutral preserves an existing approval. `reconcile` does the same against the
controller, with the check as the admission evidence.
"""

from __future__ import annotations

import dataclasses
import logging
import re

from .commands import Evidence, live_pull_request

log = logging.getLogger("tauceti_queue.eligibility")

NAME = "merge eligibility"
SCHEMA = "tauceti-merge.eligibility/v1"
REVIEW_APP_ID = 3947238
REVIEWER = "tauceti-review-bot[bot]"
# Bors's `block_labels` in TauCeti's `bors.toml`; `stacked-pr` is applied by TauCetiTools' stack policy,
# which relies on the merge backend refusing the labelled PR.
KEEP = frozenset({"keep", "hold", "wip", "human", "do-not-close", "stacked-pr"})
SHA = re.compile(r"[0-9a-f]{40}")


@dataclasses.dataclass(frozen=True)
class Decision:
    check_id: int
    pr: int
    head_sha: str
    eligible: bool
    review_safe: bool
    single: bool
    merge_base_sha: str | None
    completed_at: str = ""
    html_url: str = ""

    @property
    def conclusion(self) -> str:
        return "success" if self.eligible else ("neutral" if self.review_safe else "failure")


def trusted(check: dict, review_app_id: int = REVIEW_APP_ID) -> bool:
    return check.get("name") == NAME and ((check.get("app") or {}).get("id")) == review_app_id


def _external(check: dict) -> dict | None:
    import json

    try:
        data = json.loads(check.get("external_id") or "")
    except (TypeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def notification_pr(check: dict, repo: str, review_app_id: int = REVIEW_APP_ID) -> int | None:
    """The PR a trusted check is about, or None for any other check."""
    if not trusted(check, review_app_id):
        return None
    data = _external(check)
    if data is None or data.get("repo") != repo:
        return None
    pr = data.get("pr")
    return pr if isinstance(pr, int) and pr > 0 else None


def decode(check: dict, repo: str, pr: int, head: str) -> Decision | None:
    """Bors's `decode`: a completed trusted check whose decision is internally consistent."""
    data = _external(check)
    check_id = check.get("id")
    if (
        data is None
        or not isinstance(check_id, int)
        or check_id <= 0
        or data.get("schema") != SCHEMA
        or data.get("repo") != repo
        or data.get("pr") != pr
        or check.get("head_sha") != head
        or check.get("status") != "completed"
    ):
        return None
    eligible, review_safe, single = data.get("eligible"), data.get("review_safe"), data.get("single")
    if not all(isinstance(flag, bool) for flag in (eligible, review_safe, single)):
        return None
    if eligible and not review_safe:
        return None
    merge_base = data.get("merge_base_sha")
    if eligible and not (isinstance(merge_base, str) and SHA.fullmatch(merge_base)):
        return None
    decision = Decision(
        check_id=check_id,
        pr=pr,
        head_sha=head,
        eligible=eligible,
        review_safe=review_safe,
        single=single,
        merge_base_sha=merge_base if isinstance(merge_base, str) else None,
        completed_at=str(check.get("completed_at") or ""),
        html_url=str(check.get("html_url") or ""),
    )
    if check.get("conclusion") != decision.conclusion:
        return None
    return decision


def latest(checks: list[dict], repo: str, pr: int, head: str, review_app_id: int = REVIEW_APP_ID) -> Decision | None:
    """The authoritative decision: the trusted check with the highest id on `head` for `pr`.

    Listed with `filter=all` so another App's check of the same name cannot hide the review App's;
    the highest id wins irrespective of webhook delivery order. An invalid latest check decides
    nothing (Bors's `{:error, :invalid_eligibility_check}` is treated as waiting).
    """
    candidates = [
        c
        for c in checks
        if isinstance(c, dict)
        and trusted(c, review_app_id)
        and c.get("head_sha") == head
        and notification_pr(c, repo, review_app_id) in (None, pr)
    ]
    if not candidates:
        return None
    newest = max(candidates, key=lambda c: c.get("id") if isinstance(c.get("id"), int) else -1)
    return decode(newest, repo, pr, head)


def evidence(decision: Decision) -> Evidence:
    return Evidence(
        kind="eligibility_check",
        actor_login=REVIEWER,
        actor_id=REVIEW_APP_ID,
        created_at=decision.completed_at,
        comment_id=decision.check_id,
        comment_url=decision.html_url,
        head_sha=decision.head_sha,
        merge_base_sha=decision.merge_base_sha,
        admin=False,
        single=decision.single,
        command=f"{NAME}: {decision.conclusion}",
    )


def reconcile(
    controller, adapter, repo: str, pr: int, *, review_app_id: int = REVIEW_APP_ID, canonical_branch: str | None = None
) -> str:
    """Apply the review App's current decision for `pr` to the controller; returns the disposition.

    `approved`: the controller was asked to approve at the live head with the check as evidence;
    `revoked`: the review is unsafe and the approval withdrawn; `waiting`, `blocked` (keep label),
    `moved` (the merge base changed since the review) and `absent` (no PR) ask for nothing. The
    controller's `approve` is retry-safe, so a redelivered check cannot admit a revision twice.
    """
    live = live_pull_request(adapter, repo, pr)
    if live is None:
        return "absent"
    branch = canonical_branch or getattr(controller, "canonical_branch", None)
    branch = branch(repo) if callable(branch) else (branch or "main")
    decision = latest(adapter.eligibility_checks(repo, live.head_sha), repo, pr, live.head_sha, review_app_id)
    if decision is None:
        return "waiting"
    if not decision.review_safe:
        controller.unapprove(repo, pr, REVIEWER)
        return "revoked"
    if not decision.eligible or live.state != "open" or live.draft or live.base_ref != branch:
        return "waiting"
    labels = {label.lower() for label in live.labels}
    if labels & KEEP:
        return "blocked"
    merge_base = adapter.pr_merge_base(repo, pr, live.head_sha)
    if merge_base != decision.merge_base_sha:
        log.info(
            "eligibility for %s#%s reviewed at merge base %s, live %s; waiting",
            repo,
            pr,
            decision.merge_base_sha,
            merge_base,
        )
        return "moved"
    controller.approve(repo, pr, live.head_sha, REVIEWER, 0, evidence(decision).as_dict())
    return "approved"

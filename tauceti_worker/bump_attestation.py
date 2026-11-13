"""Attest a locally verified automatic Mathlib bump head with the `self-tested` label.

A bump head otherwise waits 2.5 to 4.5 hours for the ordinary PR build before it can be reviewed,
while main keeps moving under it. After a host bump round, whether it pushed a repair or found the
head needed none, the worker itself runs the full local verifier (`tauceti verify`, bump policy: whole-library
`lake build --iofail` against the new Mathlib, local axiom, module-system, lint and shim checks) on
that exact commit. Only when the verifier succeeds and GitHub still reports that commit as the
PR head does the worker apply `self-tested`. The TauCeti picker then decides eligibility itself
(team membership of the labeler, bump paths, real `bump-guard` validation) and posts the PR-bound
`self-tested/<pr>` assertion; the merge queue's staging build still builds and audits everything.

The agent's own checks are never taken on trust: no receipt or model statement is consulted, only
the verifier's result for the checkout whose HEAD equals the live PR head. Anything unexpected
(bubble rounds, a dirty checkout, a moved head, an unreadable PR, a failed or incomplete
verification) leaves the PR on ordinary CI.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time

from .constants import BUMP_HEAD_PREFIX, TAUCETI
from .paths import HERE
from .verification_scope import git

LABEL = "self-tested"
REVIEW_BOT = "tauceti-review-bot[bot]"
HOLD_LABELS = {"keep", "hold", "wip", "human", "on-hold", "do-not-close", "full-lint", "lint-repair"}
# A head the agent already built verifies in minutes. A longer run means the round never built it;
# ordinary CI is already running for the push, so stop rather than hold the worker for hours.
VERIFY_TIMEOUT_S = 45 * 60
# The push's own picker run removes a label it finds on a head without an assertion, so a label
# applied while that run is still in flight can vanish. Wait for the assertion and relabel once.
ASSERTION_WAIT_S = 150
ASSERTION_POLL_S = 15
LABEL_ATTEMPTS = 2


def _bump_pull(live, head: str) -> str:
    """Why `live` is not an attestable automatic bump at `head`, or "" when it is."""
    if not live:
        return "PR unreadable"
    labels = {str(label.get("name", "")).lower() for label in live.get("labels", [])}
    checks = (
        (live.get("state") == "open", "PR not open"),
        (not live.get("draft"), "PR is a draft"),
        (live.get("base", {}).get("ref") == "main", "base is not main"),
        (live.get("head", {}).get("repo", {}).get("full_name") == TAUCETI, "head is not on TauCeti"),
        (str(live.get("head", {}).get("ref", "")).startswith(BUMP_HEAD_PREFIX), "not a bump branch"),
        (live.get("user", {}).get("login") == REVIEW_BOT, "not authored by the review bot"),
        (live.get("head", {}).get("sha") == head, "PR head moved"),
        (not labels & HOLD_LABELS, "held or routed to full lint"),
    )
    return next((why for ok, why in checks if not ok), "")


def verify(w, checkout, base: str) -> dict:
    """Run the worker's verifier on `checkout` with the host build-cache view the agent used."""
    from .agents import host_build_cache_context
    from .processes import run as run_process

    env = dict(os.environ, TAUCETI_VERIFY_STATE=str(w.cfg.state), TAUCETI_VERIFY_POLICY="bump")
    env.pop("TAUCETI_ROUND_DEADLINE", None)
    logpath = w.cfg.logdir / "bump-attestation.log"
    logpath.parent.mkdir(parents=True, exist_ok=True)
    with host_build_cache_context(checkout, env, w.cfg.logdir) as prefix, logpath.open("a") as output:
        argv = [*(prefix or []), sys.executable, str(HERE / "scripts/tauceti-verify"), "--json", "--base", base]
        try:
            checked = run_process(
                argv, cwd=checkout, env=env, text=True, stdout=subprocess.PIPE, stderr=output, timeout=VERIFY_TIMEOUT_S
            )
        except subprocess.TimeoutExpired:
            return dict(status="incomplete", error=f"verification exceeded {VERIFY_TIMEOUT_S // 60} minutes")
    try:
        return json.loads(checked.stdout)
    except ValueError:
        return dict(status="incomplete", error=f"verifier produced no result; see {logpath}")


def attest(w, pr: int, *, bubble: bool, sleep=time.sleep, clock=time.monotonic) -> str:
    """Label the verified bump head left by a round `self-tested`; return a one-line outcome."""
    if bubble:
        return "skipped: bubble rounds keep ordinary CI"
    if os.environ.get("TAUCETI_BUMP_SELF_TESTED", "1") == "0":
        return "skipped: disabled by TAUCETI_BUMP_SELF_TESTED=0"
    checkout = w.cfg.checkout
    try:
        head = git(checkout, "rev-parse", "HEAD").decode().strip()
        dirty = git(checkout, "status", "--porcelain", "--untracked-files=all").decode().strip()
        base = git(checkout, "rev-parse", "--verify", "origin/main").decode().strip()
    except (OSError, subprocess.CalledProcessError) as error:
        return f"skipped: checkout unreadable ({error})"
    if dirty:
        return "skipped: checkout has uncommitted changes"
    if why := _bump_pull(w.gh.live_pull(pr), head):
        return f"skipped: {why}"
    result = verify(w, checkout, base)
    if result.get("status") != "success":
        return f"not attested: local verification {result.get('status', 'incomplete')} ({result.get('error', '')})"
    if why := _bump_pull(w.gh.live_pull(pr), head):
        return f"not attested after verification: {why}"
    context = f"self-tested/{pr}"
    for _ in range(LABEL_ATTEMPTS):
        if not w.gh.add_label(pr, LABEL):
            return "not attested: adding the label failed"
        deadline = clock() + ASSERTION_WAIT_S
        while clock() < deadline:
            sleep(ASSERTION_POLL_S)
            state = w.gh.commit_status(head, context)
            if state == "success":
                return f"attested {head[:12]}: picker accepted self-tested"
            if state in ("failure", "error"):
                return f"picker refused self-tested on {head[:12]}"
            live = w.gh.live_pull(pr)
            if why := _bump_pull(live, head):
                return f"stopped waiting: {why}"
            if LABEL not in {str(label.get("name", "")).lower() for label in live.get("labels", [])}:
                break  # removed by a concurrent picker run; label again once
    return "assertion not observed; ordinary CI continues"

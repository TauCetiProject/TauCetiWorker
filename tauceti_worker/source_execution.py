"""Dispatch native source work through existing agent and review machinery."""

from __future__ import annotations

import sys
import time
import uuid

from .config import Die, NoProgress, SourceUnavailable
from .constants import REVIEW_PREPARATION_WAIT_EXIT, REVIEW_PROVIDER_DOWN_EXIT


def still_actionable(stage, w, sv, candidate):
    if not candidate.pr:
        return stage == "roadmap"
    if candidate.source_repository != w.gh.repository or candidate.source_repository_id != w.gh.identity.native_id:
        return False
    w.gh.validate_binding()
    row = w.gh.live_pull(candidate.pr)
    if row is None:
        return None
    if row["state"] != "open" or row["head"]["sha"] != candidate.head or row.get("draft"):
        w.selection_changed = True
        return False
    labels = {label["name"].lower() for label in row.get("labels", [])}
    if labels & ({"hold", "wip", "human", "on-hold"} | ({"keep", "do-not-close"} if stage != "review" else set())):
        return False
    if stage in {"fix", "rebase", "fix-ci"}:
        actor = w.gh.authenticated_actor()
        if actor is None or row["user"]["id"] != int(actor.principal.native_id):
            return False
    if stage == "fix":
        meta = w.rs.gh_meta(candidate.pr, force=True)
        return meta.provenance in {"fresh", "missing"} and w.rs.ledger_blocking(candidate.pr, candidate.head)
    if stage == "review":
        meta = w.rs.gh_meta(candidate.pr, force=True)
        if meta.provenance not in {"fresh", "missing"}:
            return False
        if candidate.contest:
            reply = w.rs.newest_contest_reply(candidate.pr, force=True)
            return bool(
                reply
                and reply["id"] == candidate.contest_reply_id
                and reply["rubric"] == candidate.contest
                and reply["id"] > meta.data.get("replies_through", 0)
            )
        return w.rs.ledger_clean_head(candidate.pr) != candidate.head
    return True


def review(w, sv, candidate, opts, bubble):
    from .agents import run_to_logfile
    from .source_claims import lease

    if opts.work_model in ("auto", ""):
        raise Die("review needs a concrete reviewer model")
    if bubble:
        raise NoProgress("Forgejo review staging for Bubble is not implemented")
    claim = lease(w.gh, w.cfg, f"review/{candidate.pr}/{candidate.head}")
    status = claim.acquire()
    if status == 1:
        w.claims.collisions += 1
        return None
    if status != 0:
        raise SourceUnavailable("Forgejo review claim unavailable before inference")
    claim.start_heartbeat()
    work = w.cfg.state / "review-work" / uuid.uuid4().hex
    work.mkdir(parents=True, mode=0o700)
    w.cfg.logdir.mkdir(parents=True, exist_ok=True)
    log_file = w.cfg.logdir / f"review-{candidate.pr}-{time.strftime('%Y%m%d-%H%M%S')}.log"
    try:
        rc = run_to_logfile(
            [
                sys.executable,
                "-m",
                "tauceti_worker.source_review",
                "--pr",
                str(candidate.pr),
                "--head",
                candidate.head,
                "--work",
                str(work),
                "--store",
                str(w.cfg.store_dir),
                "--reviewer",
                opts.work_model,
            ],
            log_file,
            f"review {w.gh.repository.pull_url(candidate.pr)}",
        )
        if rc == 84:
            w.counters.write(f"review-err-{candidate.pr}", 0)
            w.counters.record_unchanged_review(candidate.pr, candidate.head)
            w.rs.bust(candidate.pr)
            return None
        if rc == 85:
            raise SourceUnavailable("paid review retained for native publication")
        if rc == REVIEW_PREPARATION_WAIT_EXIT:
            from .state_repair import retry_key

            w.counters.write(retry_key(candidate.pr, candidate.head), int(time.time() + 300))
            raise SourceUnavailable("source or host preparation unavailable before review inference")
        if rc == REVIEW_PROVIDER_DOWN_EXIT:
            raise NoProgress("review provider unavailable before publication")
        if rc == 82:
            w.selection_changed = True
            return None
        key = f"review-err-{candidate.pr}"
        if rc == 0:
            from .work_units import _sync_review_outbox
            from .worker_sources import shared_github_environment

            try:
                with shared_github_environment():
                    _sync_review_outbox(w, candidate.pr)
            except Exception as error:
                from .config import log

                log(f"native review archive retained for retry ({type(error).__name__})")
            w.counters.write(key, 0)
            if candidate.contest:
                w.counters.incr(f"review-contest-{candidate.pr}")
                w.counters.incr(f"review-contest-{candidate.pr}-{candidate.contest}")
        else:
            w.counters.incr(key)
        w.rs.bust(candidate.pr)
        return rc
    finally:
        claim.release()


def authorization(w, pr):
    if getattr(getattr(w, "gh", None), "worker_capable", False) is not True:
        return {}
    actor = w.gh.authenticated_actor()
    if actor is None or pr.source_author is None:
        raise NoProgress("native repair authorization unavailable")
    return {
        "source": w.gh.identity.record(),
        "actor_id": int(actor.principal.native_id),
        "author_id": int(pr.source_author.principal.native_id),
    }

"""Dispatch native source work through existing agent and review machinery."""

from __future__ import annotations

from .config import NoProgress


def still_actionable(stage, w, sv, candidate):
    """Compatibility delegate to the common freshness and work policy."""
    from .work_units import _still_actionable

    return _still_actionable(stage, w, sv, candidate)


def review(w, sv, candidate, opts, bubble):
    """Compatibility delegate; every source uses the ordinary review coordinator."""
    from .work_units import do_review

    return do_review(w, sv, candidate, opts, bubble)


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

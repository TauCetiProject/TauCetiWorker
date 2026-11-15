"""Native confirmation for the shared exact-receipt self-tested route.

Use the existing queue publisher enrollment and status format. This supplies a
PR/head-bound observation, never permission to skip candidate CI or merge.
"""

from __future__ import annotations

import re
from functools import cache
from types import SimpleNamespace

from tauceti_tools.forge import ForgeError, Incomplete

from tauceti_queue.forgejo_source import ci_status_state

from .github_transport import locked
from .native_review_publisher import QueueReviewPublisher
from .source_identity import SourceActor


def publisher(source, number, head):
    return QueueReviewPublisher(
        SimpleNamespace(source=source, number=number, head=head),
        policy_path=source.review_publication_policy,
    )


def status(source, head, context, *, publication=None, permission=None):
    """Only a current enrolled writable publisher can assert this PR/head."""
    if not re.fullmatch(r"self-tested/[1-9][0-9]*", context):
        return ""
    publication = publication or publisher(source, int(context.split("/")[1]), head)
    source.validate_binding()
    rows = source.api.statuses(source.repository, head)
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise Incomplete("native self-tested statuses are unavailable")
    rows = [row for row in rows if row.get("context") == context]
    if not rows:
        return ""
    if any(type(row.get("id")) is not int or row["id"] < 1 for row in rows):
        raise Incomplete("native self-tested status identity is unavailable")
    row = max(rows, key=lambda row: row["id"])
    if permission is None:
        # Reuse the review publisher's durable generation fence. A rolled-back
        # private policy must not revive authority the queue has retired.
        with publication.outbox() as (_store, adapter):
            permission = adapter._permission
    # No current assertion differs from a current publisher's explicit refusal:
    # fresh verification can replace an expired or untrusted observation.
    return ci_status_state(row, publication.publishers, permission, invalid="")


def confirm(source, number, head, recheck):
    """Publish once and reconcile ambiguous responses by reading, never replaying."""
    from .self_tested_label import _labels, _live_reason

    publication = publisher(source, number, head)
    with publication.outbox() as (_store, adapter):
        pass

    @cache
    def permission(actor):
        try:
            return adapter._permission(actor)
        except ForgeError as error:
            if error.status != 404:
                raise
            return "none"

    with locked(publication.ledger.with_suffix(f".self-tested.pr-{number}.lock"), timeout=30):
        source.validate_binding()
        actor = source.authenticated_actor()
        if (
            actor is None
            or actor.principal not in publication.publishers.statuses
            or permission(actor) not in {"write", "admin", "owner"}
        ):
            return "refused"

        def check(live):
            if why := _live_reason(live, head) or recheck(live):
                return why
            # Native repair already restricts authoring to the authenticated
            # contributor. Do not turn publication on someone else's PR into
            # a trusted-author assertion merely because this worker labelled it.
            if SourceActor.from_native(source.repository.forge, live.get("user")) != actor:
                return "PR author is not the enrolled worker"
            return ""

        if why := check(source.live_pull(number)):
            return f"stopped: {why}"
        context = f"self-tested/{number}"
        current = status(source, head, context, publication=publication, permission=permission)
        if current in {"failure", "error"}:
            return "refused"
        try:
            if not source.add_label(number, "self-tested"):
                return "label failed"
        except ForgeError:
            # A timed-out label mutation might already have applied. Observe it.
            live = source.live_pull(number)
            if "self-tested" not in _labels(live):
                return "label failed"
        if why := check(source.live_pull(number)):
            return f"stopped: {why}"
        if current != "success":
            try:
                source.api.request(
                    "POST",
                    source.api._repo(source.repository) + "/statuses/" + head,
                    {
                        "state": "success",
                        "context": context,
                        "description": "tauceti-ci-status/v1:" + publication.publishers.generation,
                        "target_url": source.repository.pull_url(number),
                    },
                )
            except ForgeError:
                pass  # Single attempt; the fresh read below determines what applied.
        live = source.live_pull(number)
        if why := check(live):
            return f"stopped: {why}"
        if "self-tested" not in _labels(live):
            return "unobserved"
        observed = status(source, head, context, publication=publication, permission=permission)
        return "accepted" if observed == "success" else "refused" if observed in {"failure", "error"} else "unobserved"

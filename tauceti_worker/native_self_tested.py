"""Native confirmation for the shared exact-receipt self-tested route.

Use the existing queue publisher enrollment and status format. This supplies a
PR/head-bound observation, never permission to skip candidate CI or merge.
"""

from __future__ import annotations

import re
from types import SimpleNamespace

from tauceti_tools.forge import ForgeError, Incomplete

from .native_review_publisher import QueueReviewPublisher
from .source_identity import SourceActor


def publisher(source, number, head):
    return QueueReviewPublisher(
        SimpleNamespace(source=source, number=number, head=head),
        policy_path=source.review_publication_policy,
    )


def status(source, head, context, *, publication=None):
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
    actor = SourceActor.from_native(source.repository.forge, row.get("creator"))
    if (
        actor.principal not in publication.publishers.statuses
        or publication.adapter()._permission(actor) not in {"write", "admin", "owner"}
        or row.get("description") != "tauceti-ci-status/v1:" + publication.publishers.generation
    ):
        return ""
    return row.get("status", "")


def confirm(source, number, head, recheck):
    """Publish once and reconcile ambiguous responses by reading, never replaying."""
    publication = publisher(source, number, head)
    with publication.scope():
        source.validate_binding()
        actor = source.authenticated_actor()
        if (
            actor is None
            or actor.principal not in publication.publishers.statuses
            or publication.adapter()._permission(actor) not in {"write", "admin", "owner"}
        ):
            return "refused"
        if why := recheck(source.live_pull(number)):
            return f"stopped: {why}"
        try:
            if not source.add_label(number, "self-tested"):
                return "label failed"
        except ForgeError:
            # A timed-out label mutation might already have applied. Observe it.
            live = source.live_pull(number)
            if not live or "self-tested" not in {row.get("name") for row in live.get("labels") or []}:
                return "label failed"
        if why := recheck(source.live_pull(number)):
            return f"stopped: {why}"
        context = f"self-tested/{number}"
        if status(source, head, context, publication=publication) != "success":
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
        if why := recheck(live):
            return f"stopped: {why}"
        if "self-tested" not in {row.get("name") for row in live.get("labels") or []}:
            return "unobserved"
        return "accepted" if status(source, head, context, publication=publication) == "success" else "unobserved"

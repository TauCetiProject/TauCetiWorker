"""Best-effort structured provenance for a fleet's local reporting journal."""

import fcntl
import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path


def emit(kind, *, key=None, **fields):
    if __package__:
        from .activity import emit as activity_emit
    else:
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        from tauceti_worker.activity import emit as activity_emit

    path = os.environ.get("TAUCETI_FLEET_EVENTS")
    machine = os.environ.get("TAUCETI_FLEET_MACHINE_ID")
    # Use the same identity in native receipts and the compatibility journal.
    fields = {k: v for k, v in fields.items() if k in {"repository", "pr", "head_sha", "activity", "publication"}}
    if round_id := os.environ.get("TAUCETI_ACTIVITY_ROUND_ID"):
        fields["round_id"] = round_id
    event = dict(
        version=1,
        machine_id=machine,
        worker=os.environ.get("TAUCETI_WORKER_ID", "default"),
        kind=kind,
        at=time.time(),
        **fields,
    )
    identity = key or json.dumps(event, sort_keys=True)
    event["id"] = hashlib.sha256(((machine or "local") + ":" + kind + ":" + identity).encode()).hexdigest()
    activity_emit(kind, key=event["id"], at=event["at"], **fields)
    if not path or not machine:
        return
    try:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "a") as stream:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            stream.write(json.dumps(event, sort_keys=True) + "\n")
            stream.flush()
    except (OSError, ValueError, TypeError):
        pass  # Statistics never change whether a GitHub mutation succeeds.


def mutation(value, result):
    args = value.get("argv", [])
    text = result.stdout or ""
    created = args[:2] == ["pr", "create"] or any(re.fullmatch(r"/?repos/[^/]+/[^/]+/pulls", a) for a in args)
    match = re.search(r"https://github.com/([^/\s]+/[^/\s]+)/pull/(\d+)", text)
    if not match:
        try:
            data = json.loads(text)
            match = re.search(r"https://github.com/([^/\s]+/[^/\s]+)/pull/(\d+)", data.get("html_url", ""))
        except (ValueError, TypeError, AttributeError):
            pass
    if created and match:
        emit("pr_created", key=match[0], repository=match[1], pr=int(match[2]))
        return
    from .github_outbox import target

    repository, pr = target(value)
    if repository and pr:
        publication = None
        method = next((args[i + 1] for i, arg in enumerate(args[:-1]) if arg in ("-X", "--method")), "")
        method = next((arg.split("=", 1)[1] for arg in args if arg.startswith("--method=")), method).upper()
        if args[:2] == ["pr", "close"]:
            publication = "closure"
        elif method in ("", "POST") and (
            args[:2] == ["pr", "comment"]
            and not {"--edit-last", "--delete-last"}.intersection(args)
            or any(
                re.fullmatch(
                    r"/?repos/[^/]+/[^/]+/(?:issues|pulls)/(?:\d+/)?comments(?:/\d+(?:/replies)?)?", a.split("?", 1)[0]
                )
                for a in args
            )
        ):
            publication = "reply"
        elif args[:2] == ["api", "graphql"]:
            query = " ".join(a for a in args if a.startswith("query="))
            if any(
                name in query
                for name in (
                    "addPullRequestReviewThreadReply",
                    "addComment",
                )
            ):
                publication = "reply"
            elif "closePullRequest" in query:
                publication = "closure"
        emit(
            "pr_activity",
            key=value.get("created", "").__str__(),
            repository=repository,
            pr=pr,
            activity="github_mutation",
            publication=publication,
        )


if __name__ == "__main__":
    if sys.argv[1:] == ["pushed"]:
        pr = os.environ.get("TAUCETI_GITHUB_PR", "")
        repo = os.environ.get("TAUCETI_GITHUB_REPOSITORY", "TauCetiProject/TauCeti")
        if pr.isdigit():
            emit("pr_activity", repository=repo, pr=int(pr), activity="branch_push")

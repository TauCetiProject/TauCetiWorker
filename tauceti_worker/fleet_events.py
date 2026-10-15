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
    path = os.environ.get("TAUCETI_FLEET_EVENTS")
    machine = os.environ.get("TAUCETI_FLEET_MACHINE_ID")
    if not path or not machine:
        return
    # No command lines, prompts, request payloads or credentials enter this log.
    fields = {k: v for k, v in fields.items() if k in {"repository", "pr", "head_sha", "activity"}}
    event = dict(
        version=1,
        machine_id=machine,
        worker=os.environ.get("TAUCETI_WORKER_ID", "default"),
        kind=kind,
        at=time.time(),
        **fields,
    )
    identity = key or json.dumps(event, sort_keys=True)
    event["id"] = hashlib.sha256((machine + ":" + kind + ":" + identity).encode()).hexdigest()
    try:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "a") as stream:
            fcntl.flock(stream, fcntl.LOCK_EX)
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
    context = value.get("context", {})
    if context.get("repository") and str(context.get("pr", "")).isdigit():
        emit(
            "pr_activity",
            key=value.get("created", "").__str__(),
            repository=context["repository"],
            pr=int(context["pr"]),
            activity="github_mutation",
        )


if __name__ == "__main__":
    if sys.argv[1:] == ["pushed"]:
        pr = os.environ.get("TAUCETI_GITHUB_PR", "")
        repo = os.environ.get("TAUCETI_GITHUB_REPOSITORY", "TauCetiProject/TauCeti")
        if pr.isdigit():
            emit("pr_activity", repository=repo, pr=int(pr), activity="branch_push")

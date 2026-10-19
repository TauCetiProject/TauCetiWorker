"""Usage evidence joins. Invocation IDs come from admissions, never transcript guesses."""

from decimal import Decimal

from .usage_accounting import session_usage


def evidence(directory, session, sessions):
    value = session_usage(directory, session)
    if value is None:
        value = linked_transcript(directory, session)
    if value is None:
        return None
    # A resumed CLI can replay old responses. Charge each provider response once,
    # to the first admitted invocation which recorded it.
    seen = set()
    previous = sessions.values() if session.get("resume") else ()
    for prior in previous:
        if prior["id"] == session["id"]:
            break
        if not prior.get("bridge") or (
            prior.get("resume") != session["resume"] and session["resume"] not in prior.get("totals", {})
        ):
            continue
        saved = session_usage(directory, prior)
        if saved:
            seen.update((m["session"], m["message_id"]) for m in saved["messages"].values())
    items = [m for m in value["messages"].values() if (m["session"], m["message_id"]) not in seen]
    known = sum((Decimal(m["cost"]) for m in items if m["cost"] is not None), Decimal(0))
    complete = (
        bool(items)
        and value["closed"]
        and not value["inflight"]
        and all(m["complete"] and m["cost"] is not None for m in items)
    )
    return dict(items=items, cost=known, complete=complete, checkpoint=value)


def linked_transcript(directory, session):
    """Legacy fallback only when a receipt names the exact provider session.

    Never infer identity from a worker/time overlap. Reject duplicate files, filter
    pre-admission responses on resumes, and persist only content-free evidence.
    """
    import hashlib
    import json
    import os
    import stat
    import time
    import uuid
    from datetime import datetime
    from pathlib import Path

    from .claude_api import safe_message
    from .paths import HERE
    from .usage_accounting import MAX_CHECKPOINT, UsageJournal

    try:
        receipt = safe_message(Path(session["bridge"]) / "inbox" / (session["id"] + ".receipt"))
        providers = list(receipt.get("lower_bound_totals", {}))
        if receipt.get("id") != session["id"] or not providers or any(str(uuid.UUID(p)) != p for p in providers):
            return None
        worker = session["worker"]
        if not worker or Path(worker).name != worker:
            return None
        roots = [HERE / "state" / worker / "home/.claude/projects"]
        if worker == "default":
            roots.append(Path.home() / ".claude/projects")
        if override := os.environ.get("TAUCETI_USAGE_TRANSCRIPT_ROOTS"):
            roots = [Path(p) for p in override.split(os.pathsep)]
        journal = UsageJournal.__new__(UsageJournal)
        at = time.time()
        journal.clock = lambda: at
        journal.value = dict(
            version=1,
            id=session["id"],
            model=session["model"],
            started=session["started"],
            observed_at=session["started"],
            messages={},
            inflight=True,
            closed=False,
            sequence=0,
            prices_sha256=hashlib.sha256(Path(__file__).with_name("prices.json").read_bytes()).hexdigest(),
            recovered_from="receipt-linked-native-transcript",
            sources=[],
        )
        journal.current, journal.waiting, journal.dirty = {}, {"unknown"}, set()
        journal.flush = lambda: None
        for provider in providers:
            matches = {
                p.resolve() for root in roots for p in root.glob("*/" + provider + ".jsonl") if not p.is_symlink()
            }
            if len(matches) != 1:
                return None
            path = matches.pop()
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd, "rb") as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_size > 128 * 1024**2:
                    return None
                for raw in stream:
                    try:
                        event = json.loads(raw)
                    except ValueError:
                        continue  # an interrupted final line supplies no evidence
                    if event.get("type") != "assistant" or event.get("sessionId") != provider:
                        continue
                    try:
                        at = datetime.fromisoformat(event["timestamp"].replace("Z", "+00:00")).timestamp()
                    except (ValueError, KeyError, TypeError):
                        return None
                    if session["started"] <= at <= time.time():
                        journal.observe(dict(type="assistant", session_id=provider, message=event.get("message")))
            journal.value["sources"].append(
                dict(provider=provider, file=str(path), size=info.st_size, mtime_ns=info.st_mtime_ns)
            )
        if len(json.dumps(journal.value)) > MAX_CHECKPOINT or not journal.value["messages"]:
            return None
        # CLI termination and unobserved requests cannot be proved from a legacy
        # transcript; retain uncertainty even when its last response is complete.
        journal.value["inflight"] = True
        return journal.value
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return None

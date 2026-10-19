"""Recover operational failures in one owned inspection, never reroll a source verdict."""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

from .audit_alerts import notify_block, save
from .config import Die, NoProgress, log

# The reviewed version-1 launcher deployed before worker-bound judgments. This
# identifies a protocol migration, not an exception for a particular commit typo.
V1_PROFILE = {
    "launcher_code_digest": "f2f06b8029f3b13ef35d9669c5e2bcd3a64b0f1d2b0b296fc9251f256e0daedd",
    "reader_digest": "f852a254efc4336cce71f853b8c86e3ccc87084f2914efbf085463c3bb57bfc0",
    "catalog_digest": "e16b921e0a354d549be4aeb439385f0fa82962eb948210f4c27ac9811a095ba3",
}


def safe_judgment(value, batch: dict, paged: bool, *, legacy=False, allow_empty=False) -> bool:
    """Recognize only a pass or, for bounded immediate repair, an empty incomplete."""
    if not isinstance(value, dict) or value.get("findings") != []:
        return False
    keys = {"verdict", "findings"} | ({"read_token"} if paged else set())
    if legacy and "input_digest" in value:
        keys |= {"input_digest", "commits"}
        if value["input_digest"] != batch["input_digest"] or not isinstance(value.get("commits"), list):
            return False
        if any(not isinstance(sha, str) for sha in value["commits"]):
            return False
    return set(value) == keys and (
        value.get("verdict") == "pass" or (allow_empty and value.get("verdict") == "incomplete")
    )


def safe_history(work: Path, batch: dict, paged: bool, *, legacy=False, allow_empty=False) -> bool:
    from . import security_audit as sa

    try:
        rollouts = sa.native_rollouts(work)
        if len(rollouts) != 1:
            return False
        for line in rollouts[0].read_text().splitlines():
            row = json.loads(line)
            item = row.get("payload", {})
            if row.get("type") != "response_item" or item.get("type") != "message" or item.get("role") != "assistant":
                continue
            text = "".join(p.get("text", "") for p in item.get("content", []) if p.get("type") == "output_text")
            if item.get("phase") == "commentary":
                if '"verdict"' in text:
                    return False
            elif item.get("phase") not in ("final", "final_answer") or not safe_judgment(
                json.loads(text), batch, paged, legacy=legacy, allow_empty=allow_empty
            ):
                return False
        return True
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return False


def mechanical_output(
    transcript: str, batch: dict, receipt: Path, paged: bool, work: Path, *, legacy_history=False, allow_empty=True
) -> bool:
    """Validate everything except a known mechanical final-output failure. Never approve it."""
    from . import security_audit as sa

    try:
        events = [json.loads(line) for line in transcript.splitlines() if line.strip()]
        if any(
            e.get("type")
            not in {
                "thread.started",
                "turn.started",
                "turn.completed",
                "item.started",
                "item.updated",
                "item.completed",
            }
            for e in events
        ):
            return False
        thread = sa.retained_thread(work)
        if any(e.get("type") == "thread.started" and e.get("thread_id") != thread for e in events):
            return False
        if not safe_history(work, batch, paged, legacy=legacy_history, allow_empty=allow_empty):
            return False
        value = sa.codex_final(transcript, paged)
        if not safe_judgment(value, batch, paged, legacy=legacy_history, allow_empty=allow_empty):
            return False
        finals = []
        for line in sa.native_rollouts(work)[0].read_text().splitlines():
            row = json.loads(line)
            item = row.get("payload", {})
            if (
                row.get("type") == "response_item"
                and item.get("type") == "message"
                and item.get("role") == "assistant"
                and item.get("phase") in ("final", "final_answer")
            ):
                finals.append(
                    json.loads(
                        "".join(p.get("text", "") for p in item.get("content", []) if p.get("type") == "output_text")
                    )
                )
        if not finals or finals[-1] != value:
            return False
        # A complete receipt is mandatory even when the model's final token is
        # damaged. A fresh answer must still present the actual delivery token.
        proof = json.loads(receipt.read_text()) if paged else {}
        probe = sa.bind_verdict({"verdict": "pass", "findings": []}, batch)
        sa.validate_reads(probe, batch, receipt, paged, proof.get("last_token"))
        original = dict(value)
        token = original.pop("read_token", None) if paged else None
        try:
            judgment = (
                sa.validate_verdict(original, batch) if "input_digest" in original else sa.bind_verdict(original, batch)
            )
            sa.validate_reads(judgment, batch, receipt, paged, token)
        except (Die, ValueError, TypeError, KeyError):
            return True
        return False  # An already valid answer is never a reason to ask again.
    except (Die, OSError, ValueError, TypeError, KeyError, AttributeError):
        return False


def retained(work: Path, previous: dict, *, seen: set[Path] | None = None) -> tuple[Path, dict, dict, bool]:
    """Check owned input, launcher, lineage and all prior judgments before any model call."""
    from . import security_audit as sa

    original = Path(previous["attempt"]).resolve()
    seen = set() if seen is None else seen
    if original in seen:
        raise Die("security-audit: cyclic recovery lineage")
    seen.add(original)
    if original.parent != work.resolve() or not original.name.startswith("attempt-"):
        raise Die("security-audit: recovery attempt is outside this worker's state")
    batch = json.loads((original / "input.json").read_text())
    digest = hashlib.sha256(sa.serialize({k: v for k, v in batch.items() if k != "input_digest"}).encode()).hexdigest()
    if digest != batch["input_digest"]:
        raise Die("security-audit: retained input digest mismatch")
    if previous["from_sha"] != batch["from_sha"] or previous["to_sha"] != batch["commits"][-1]["sha"]:
        raise Die("security-audit: recovery range changed")
    if (
        not (original / "execution.json").exists()
        and previous.get("reason") == "security-audit: operational recovery interrupted"
    ):
        # The execution record is written before subprocess launch. With none,
        # no new request ran; preserve this attempt and use its verified parent.
        parent = json.loads((original / "previous-block.json").read_text())
        ancestor, ancestor_batch, execution, legacy = retained(work, parent, seen=seen)
        if ancestor_batch != batch:
            raise Die("security-audit: interrupted recovery input changed")
        return ancestor, batch, execution, legacy
    execution = json.loads((original / "execution.json").read_text())
    legacy = execution.get("protocol_version", 1) == 1
    if legacy:
        if any(execution.get(k) != v for k, v in V1_PROFILE.items()):
            raise Die("security-audit: unknown legacy inspection profile")
        policy = sa.PROMPT
    else:
        if execution.get("protocol_version") != sa.PROTOCOL_VERSION:
            raise Die("security-audit: unknown judgment protocol")
        identity = {"input_digest": digest, "commits": [c["sha"] for c in batch["commits"]]}
        if execution.get("batch_identity") != identity:
            raise Die("security-audit: retained launch belongs to another batch")
        profile = sa.inspection_profile()
        if any(execution.get(k) != profile[k] for k in ("catalog_digest", "reader_digest")):
            raise Die("security-audit: retained inspection policy or reader changed")
        policy = sa.MODEL_PROMPT
        if execution.get("legacy_history"):
            parent = json.loads((original / "previous-block.json").read_text())
            ancestor, ancestor_batch, _, migrated = retained(work, parent, seen=seen)
            if ancestor_batch != batch or sa.retained_thread(ancestor) != sa.retained_thread(original) or not migrated:
                raise Die("security-audit: invalid legacy continuation lineage")
            legacy = True
    if execution.get("prompt_digest") != hashlib.sha256((original / "prompt.txt").read_bytes()).hexdigest():
        raise Die("security-audit: retained inspection prompt digest mismatch")
    sa.validate_output_profile(
        execution, original, batch, policy_prompt=policy, durable=not (execution.get("protocol_version", 1) == 1)
    )
    paged = len(sa.serialize(batch).encode()) > sa.INLINE_BYTES
    if not safe_history(original, batch, paged, legacy=legacy):
        raise Die("security-audit: previous findings or uncertainty forbid automatic recovery")
    for line in (work / "ledger.jsonl").read_text().splitlines():
        row = json.loads(line)
        if row.get("batch") == digest and "verdict" in row and row["verdict"]["verdict"] != "pass":
            raise Die("security-audit: a recorded source verdict remains binding")
    return original, batch, execution, legacy


def recover(cfg, opts, previous: dict) -> bool:
    """At most one recovery invocation per cooldown; retain the same native UUID."""
    from . import security_audit as sa

    work = cfg.state / sa.CURSOR
    try:
        original, batch, execution, legacy = retained(work, previous)
        paged = execution["paged"]
        transcript_path = original / "model-transcript.jsonl"
        transcript = transcript_path.read_text() if transcript_path.exists() else ""
        mechanical = mechanical_output(
            transcript, batch, original / "read-receipt.json", paged, original, legacy_history=legacy, allow_empty=False
        )
        service = (
            sa.codex_service_error(transcript, paged, original, batch, protocol=2, legacy_history=legacy) is not None
        )
        interrupted = previous.get("reason") == "security-audit: operational recovery interrupted"
        timed_out = previous.get("failure_kind") == "provider-timeout"
        if not mechanical and not service and not interrupted and not timed_out:
            return False
    except (Die, OSError, ValueError, KeyError, TypeError, AttributeError):
        return False
    state_path = work / "operational-recovery.json"
    try:
        state = json.loads(state_path.read_text()) if state_path.exists() else {}
        key = batch["input_digest"] + ":" + sa.retained_thread(original)
        entry = state.get(key, {})
        if (
            not isinstance(entry, dict)
            or type(entry.get("attempts", 0)) is not int
            or entry.get("attempts", 0) < 0
            or type(entry.get("retry_at", 0)) not in (int, float)
        ):
            raise ValueError("invalid recovery budget")
    except (OSError, ValueError, TypeError, AttributeError) as exc:
        raise NoProgress("security-audit: retained recovery budget is unreadable; preserve it for inspection") from exc
    if entry.get("retry_at", 0) > time.time():
        raise NoProgress("security-audit: retained operational recovery is waiting for its cooldown")
    from .loop import resolve_work_model

    resolve_work_model(cfg, "codex", dry=False, ignore_quota=getattr(opts, "ignore_quota", False), fresh=True)
    attempts = entry.get("attempts", 0) + 1
    state[key] = {"attempts": attempts, "retry_at": time.time() + min(3600, 300 * 2 ** min(attempts - 1, 4))}
    save(state_path, state)  # Reserve the budget before launch; a restart cannot reset it.
    sa.require_admin()
    if sa.remote_sha(sa.CURSOR) != batch["from_sha"]:
        raise NoProgress("security-audit: cursor moved before recovery launch")
    main = sa.remote_sha("main")
    sa.refresh(work / "objects.git", main)
    target = batch["commits"][-1]["sha"]
    remaining = sa.validate_history(work / "objects.git", batch["from_sha"], main, target)
    if remaining[: len(batch["commits"])] != [c["sha"] for c in batch["commits"]]:
        raise Die("security-audit: retained batch is not the next main prefix")
    actor = sa.api("user")["login"]
    attempt = work / f"attempt-{time.time_ns()}"
    attempt.mkdir()
    (attempt / "input.json").write_text(sa.serialize(batch))
    (attempt / "previous-block.json").write_text(sa.serialize(previous))
    recovery = {
        "actor": actor,
        "batch": batch["input_digest"],
        "from_sha": batch["from_sha"],
        "to_sha": target,
        "continuation_of": str(original),
        "attempt": str(attempt),
        "recovery_kind": "operational-v2",
    }
    sa.record(work / "ledger.jsonl", recovery)
    # Journal the in-flight attempt before launch. Its native history and receipt
    # are durable, so a killed Worker resumes it rather than losing new findings.
    save(
        work / "blocked.json",
        {
            "from_sha": batch["from_sha"],
            "to_sha": target,
            "attempt": str(attempt),
            "continuation_of": str(original),
            "reason": "security-audit: operational recovery interrupted",
        },
    )
    log(f"security-audit: automatic same-session recovery {batch['from_sha'][:12]}..{target[:12]}")
    try:
        verdict = sa.inspect(
            batch, "codex", attempt, resume_from=original, legacy_history=legacy, repair_output=mechanical
        )
        sa.record(work / "ledger.jsonl", {**recovery, "verdict": verdict, "protocol_version": sa.PROTOCOL_VERSION})
        if verdict["verdict"] != "pass":
            raise Die("security-audit: " + "; ".join(verdict["findings"]))
        landed = sa.advance(work / "objects.git", batch)
        sa.record(work / "ledger.jsonl", {**recovery, "advanced": landed})
        (work / "blocked.json").rename(work / f"resolved-operational-block-{time.time_ns()}.json")
        log(f"security-audit: recovered inspection passed; advanced to {landed}")
        sa.dispatch_advanced(work, landed)
    except (Die, NoProgress, OSError, ValueError, TypeError, AttributeError, sa.subprocess.TimeoutExpired) as exc:
        failure = {
            "from_sha": batch["from_sha"],
            "to_sha": target,
            "attempt": str(attempt),
            "continuation_of": str(original),
            "reason": str(exc),
        }
        if isinstance(exc, sa.subprocess.TimeoutExpired):
            failure["failure_kind"] = "provider-timeout"
        if not (attempt / "execution.json").exists():
            failure["setup_attempt"] = str(attempt)
            failure["attempt"] = str(original)
        save(work / "blocked.json", failure)
        sa.record(work / "ledger.jsonl", failure)
        notify_block(work, failure)
        raise NoProgress(f"security-audit: retained recovery did not advance: {exc}") from exc
    return True

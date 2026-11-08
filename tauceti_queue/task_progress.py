"""Small, optional observations made from an already-loaded private task checkpoint.

The lease binding stays private. Only ``public()`` crosses the monitoring boundary.
Task/member context, prompts, diagnostics and logs are never traversed or copied.
"""

from __future__ import annotations

import math
import re
from datetime import UTC, datetime
from pathlib import Path

SCHEMA = "tauceti.queue.task-progress/v1"
FILENAME = "progress.json"
TOKEN_KEYS = ("input_tokens", "cached_input_tokens", "output_tokens", "reasoning_output_tokens", "total_tokens")
PROVIDERS = frozenset({"codex", "claude", "openai", "anthropic", "gemini"})
MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,99}\Z")
# Display sampling bounds the projection, never the work or accounting of attempts.
DISPLAY_MODELS = 20


def numeric(value):
    return value if type(value) in (int, float) and math.isfinite(value) and value >= 0 else None


def text(value):
    return re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", value)[:40] if isinstance(value, str) else ""


def model(value):
    return isinstance(value, str) and bool(MODEL.fullmatch(value)) and ".." not in value


def summarize(raw, final=None):
    """Aggregate every recorded attempt with constant-size totals and a bounded model display."""
    task = raw.get("task") or {}
    usage = {key: {"recorded_tokens": 0, "recorded_attempts": 0, "unknown_attempts": 0} for key in TOKEN_KEYS}
    cost = {"recorded_usd": 0, "recorded_attempts": 0, "unknown_attempts": 0, "coverage": "recorded_subtotal_only"}
    providers, models = set(), set()
    truncated = False

    final = final if final is not None else raw.get("outcome") or raw
    pending = [iter((final,))]
    while pending:
        try:
            value = next(pending[-1])
        except StopIteration:
            pending.pop()
            continue
        if isinstance(value, list):
            pending.append(iter(value))
        elif isinstance(value, dict):
            if "telemetry" in value or "estimated_cost_usd" in value:
                amount = numeric(value.get("estimated_cost_usd"))
                cost["recorded_usd"] += amount or 0
                cost["recorded_attempts" if amount is not None else "unknown_attempts"] += 1
                recorded = (value.get("telemetry") or {}).get("usage") or {}
                for key, counts in usage.items():
                    amount = recorded.get(key)
                    if type(amount) is int and amount >= 0:
                        counts["recorded_tokens"] += amount
                        counts["recorded_attempts"] += 1
                    else:
                        counts["unknown_attempts"] += 1
                if value.get("provider") in PROVIDERS:
                    providers.add(value["provider"])
                name = value.get("model")
                if model(name) and name not in models:
                    if len(models) < DISPLAY_MODELS:
                        models.add(name)
                    else:
                        truncated = True
            else:
                # Outcomes repeat parallel results in groups, flattened attempts and agent.
                # Follow the canonical attempt list once, including all entries in it.
                agents = value.get("agent")
                parallel = value.get("parallel")
                if isinstance(agents, list) and agents and isinstance(parallel, dict):
                    children = [agents]
                    if not any(isinstance(run, dict) and run.get("parallel") is True for run in agents):
                        # Failed parallel work may precede a sequential repair without being
                        # copied into agent[]. Its recorded attempts still count.
                        children.append(parallel)
                    pending.append(iter(children))
                    continue
                for name in ("agent", "attempts", "parallel", "groups", "result"):
                    child = value.get(name)
                    if isinstance(child, dict | list) and child:
                        pending.append(iter((child,)))
                        break
    return {
        "schema": SCHEMA,
        "task_id": task.get("task_id"),
        "claim_token": task.get("claim_token"),
        "state": text(raw.get("state")),
        "updated_at": numeric(raw.get("updated_at")),
        "seconds": numeric(final.get("seconds")),
        "providers": sorted(providers),
        "models": sorted(models),
        "models_truncated": truncated,
        "usage": usage,
        "cost": cost,
    }


def public(record):
    """Reapply the allowlist when reading a local projection; never publish its lease binding."""
    if record.get("schema") != SCHEMA:
        raise ValueError("unknown task progress schema")
    observed = numeric(record.get("updated_at"))
    usage = {
        key: {
            name: numeric((record.get("usage", {}).get(key) or {}).get(name))
            for name in ("recorded_tokens", "recorded_attempts", "unknown_attempts")
        }
        for key in TOKEN_KEYS
    }
    return {
        "coverage": "observed",
        "state": text(record.get("state")),
        "observed_at": datetime.fromtimestamp(observed, UTC).isoformat().replace("+00:00", "Z")
        if observed is not None
        else None,
        "seconds": numeric(record.get("seconds")),
        "providers": sorted({value for value in record.get("providers", []) if value in PROVIDERS}),
        "models": sorted({value for value in record.get("models", []) if model(value)})[:DISPLAY_MODELS],
        "models_truncated": record.get("models_truncated") is True,
        "usage": usage,
        "cost": {
            **{
                name: numeric((record.get("cost") or {}).get(name))
                for name in ("recorded_usd", "recorded_attempts", "unknown_attempts")
            },
            "coverage": "recorded_subtotal_only",
        },
    }


def write(directory: Path, raw, final=None):
    """Progress is disposable: storage or telemetry failures must never fail a repair."""
    from tauceti_worker.runtime_status import atomic_json

    try:
        atomic_json(directory / FILENAME, summarize(raw, final), durable=False)
    except Exception:
        pass

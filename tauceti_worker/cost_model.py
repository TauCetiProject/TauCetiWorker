"""Read-only cost and scaling estimates from Tau Ceti's local agent transcripts.

The analyzer deliberately reports physical quantities separately from prices.  Local logs can
measure wall time and token traffic; converting those into CPU-hours or dollars requires explicit
assumptions about parallelism and the runner/provider price.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
import math
import re
import statistics
from collections import defaultdict
from pathlib import Path
from typing import Iterable


SESSION_RE = re.compile(r"^\[session\]\s+(codex|claude)\s+([0-9a-f-]+)(?:\s+model=([^\s]+))?", re.M)
WALL_RE = re.compile(r"Wall time(?::|\s)(?:\s*)([0-9.]+)\s*seconds", re.I)
PROCESS_RE = re.compile(r"(?:session ID|cell ID)\s+([A-Za-z0-9_-]+)", re.I)
CLAIM_RE = re.compile(r'claim\.sh\s+acquire\s+["\']author/([^/"\']+)/')


@dataclasses.dataclass
class ToolCall:
    command: str
    name: str
    started: dt.datetime
    seconds: float
    category: str | None
    mutates: bool


@dataclasses.dataclass
class Session:
    provider: str
    session_id: str
    model: str | None
    phase: str
    agent_log: Path
    transcript: Path | None
    started: dt.datetime | None
    ended: dt.datetime | None
    tools: list[ToolCall]
    tokens: dict[str, int]
    roadmap_area: str | None
    roadmap_bytes: int | None
    orientation_to_claim_seconds: float | None
    orientation_to_edit_seconds: float | None


def _time(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _percentile(values: Iterable[float], fraction: float) -> float | None:
    xs = sorted(values)
    if not xs:
        return None
    return xs[min(len(xs) - 1, max(0, math.ceil(fraction * len(xs)) - 1))]


def distribution(values: Iterable[float]) -> dict[str, float | int | None]:
    xs = list(values)
    return {
        "n": len(xs),
        "mean": statistics.fmean(xs) if xs else None,
        "p25": _percentile(xs, 0.25),
        "median": statistics.median(xs) if xs else None,
        "p75": _percentile(xs, 0.75),
        "p90": _percentile(xs, 0.9),
    }


def _phase(text: str) -> str:
    if "You are authoring a new pull request" in text:
        return "preparation"
    if "You are addressing AI code review" in text:
        return "revision"
    if "You are fixing FAILING CI" in text:
        return "fix-ci"
    if "You are reconciling the branch with current main" in text:
        return "rebase"
    if "You are adapting TauCetiProject/TauCeti" in text and "Mathlib bump" in text:
        return "bump"
    return "other"


def _category(command: str) -> str | None:
    # Match programs at shell-command boundaries. Looking for arbitrary substrings would count
    # inspection commands such as `rg "lake build"` as builds, which is both common and wrong.
    unquoted = []
    quote = None
    escaped = False
    for char in command:
        if escaped:
            unquoted.append(" ")
            escaped = False
        elif char == "\\":
            unquoted.append(" ")
            escaped = True
        elif quote:
            unquoted.append(" ")
            if char == quote:
                quote = None
        elif char in "'\"":
            unquoted.append(" ")
            quote = char
        else:
            unquoted.append(char)
    command = "".join(unquoted)
    start = r"(?:^|[\n;&|])\s*(?:(?:if|then|elif|do)\s+)?!?\s*(?:[A-Za-z_][A-Za-z0-9_]*=[^\s;&|]+\s+)*"
    kinds = []
    if re.search(start + r"lake\s+exe\s+cache\s+get!?\b|" + start + r"lake\s+cache\s+get\b", command):
        kinds.append("cache-get")
    if re.search(start + r"lake\s+build\b", command):
        kinds.append("lake-build")
    if re.search(start + r"lake\s+exe\s+axioms\b", command):
        kinds.append("axioms")
    if re.search(start + r"(?:lake\s+env\s+)?lean\s+[^;&|\n]*\.lean\b", command):
        kinds.append("lean-check")
    if not kinds:
        return None
    return kinds[0] if len(kinds) == 1 else "mixed-lean"


def _mutates(name: str, command: str) -> bool:
    if name.lower() in {"edit", "write", "apply_patch", "notebookedit"}:
        return True
    return bool(
        re.search(
            r"\bapply_patch\b|\bsed\s+-i\b|\bperl\s+-[^\s]*i|\b(?:mv|cp|touch)\s+|"
            r"(?:^|[;&|]\s*)(?:cat|printf|echo)\b[^\n]*(?:>|\btee\b)|"
            r"\.write_(?:text|bytes)\s*\(",
            command,
        )
    )


def _command_from_codex(name: str, raw: str) -> str:
    try:
        value = json.loads(raw)
        if isinstance(value, dict):
            return str(value.get("cmd") or value.get("input") or raw)
    except (json.JSONDecodeError, TypeError):
        pass
    # Modern Codex wraps exec_command in a short JavaScript program.
    match = re.search(r'tools\.exec_command\(\s*(\{.*?\})\s*\)', raw, re.S)
    if match:
        try:
            value = json.loads(match.group(1))
            return str(value.get("cmd") or raw)
        except json.JSONDecodeError:
            pass
    return raw


def _output_text(value) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(str(x.get("text", "")) for x in value if isinstance(x, dict))
    return json.dumps(value, default=str)


def _parse_codex(path: Path) -> tuple[list[ToolCall], dict[str, int], dt.datetime | None, dt.datetime | None]:
    pending: dict[str, tuple[str, str, dt.datetime]] = {}
    process_owner: dict[str, ToolCall] = {}
    tools: list[ToolCall] = []
    tokens: dict[str, int] = {}
    modern_commands: list[ToolCall] = []
    first = last = None
    with path.open(errors="replace") as handle:
        for line in handle:
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            when = _time(row.get("timestamp"))
            if when:
                first = first or when
                last = when
            payload = row.get("payload") or {}
            typ = payload.get("type")
            if row.get("type") == "event_msg" and typ == "item_completed":
                item = payload.get("item") or {}
                if item.get("type") == "CommandExecution":
                    command_value = item.get("command") or []
                    if isinstance(command_value, list):
                        command = str(command_value[-1]) if command_value else ""
                    else:
                        command = str(command_value)
                    started_ms = item.get("started_at_ms")
                    completed_ms = item.get("completed_at_ms")
                    duration = item.get("duration") or {}
                    if isinstance(started_ms, (int, float)):
                        command_started = dt.datetime.fromtimestamp(started_ms / 1000, dt.timezone.utc)
                    else:
                        command_started = when
                    if isinstance(completed_ms, (int, float)) and isinstance(started_ms, (int, float)):
                        seconds = max(0.0, (completed_ms - started_ms) / 1000)
                    else:
                        seconds = float(duration.get("secs", 0)) + float(duration.get("nanos", 0)) / 1e9
                    if command_started:
                        modern_commands.append(ToolCall(
                            command, "exec_command", command_started, seconds,
                            _category(command), _mutates("exec_command", command),
                        ))
            if row.get("type") == "event_msg" and typ == "token_count":
                usage = ((payload.get("info") or {}).get("total_token_usage") or {})
                tokens = {k: int(v) for k, v in usage.items() if isinstance(v, (int, float))}
            if typ in {"function_call", "custom_tool_call"} and when:
                call_id = payload.get("call_id")
                name = str(payload.get("name") or "")
                raw = str(payload.get("arguments") or payload.get("input") or "")
                if call_id:
                    pending[call_id] = (name, raw, when)
            elif typ in {"function_call_output", "custom_tool_call_output"} and when:
                call_id = payload.get("call_id")
                if not call_id or call_id not in pending:
                    continue
                name, raw, started = pending.pop(call_id)
                if name == "exec":
                    nested = re.search(r"\btools\.(exec_command|write_stdin|wait)\s*\(", raw)
                    if nested:
                        name = f"wrapped-{nested.group(1)}"
                out = _output_text(payload.get("output"))
                wall = WALL_RE.search(out)
                seconds = float(wall.group(1)) if wall else max(0.0, (when - started).total_seconds())
                command = _command_from_codex(name, raw)
                # Polls belong to the original long-running command, not to a new tool category.
                poll_id = None
                if name in {"write_stdin", "wait", "wrapped-write_stdin", "wrapped-wait"}:
                    try:
                        args = json.loads(raw)
                        poll_id = str(args.get("session_id") or args.get("cell_id") or "")
                    except json.JSONDecodeError:
                        match = re.search(r"\b(?:session_id|cell_id)\s*:\s*['\"]?([A-Za-z0-9_-]+)", raw)
                        poll_id = match.group(1) if match else None
                if poll_id and poll_id in process_owner:
                    process_owner[poll_id].seconds += seconds
                    continue
                tool = ToolCall(command, name, started, seconds, _category(command), _mutates(name, command))
                tools.append(tool)
                process = PROCESS_RE.search(out)
                if process:
                    process_owner[process.group(1)] = tool
    if modern_commands:
        # Current Codex emits an exact CommandExecution item for every unified `exec` wrapper.
        # Drop only shell wrappers and their polls. Other calls such as tools.apply_patch are also
        # wrapped in outer `exec` records but have no CommandExecution counterpart and must survive.
        duplicated = {"wrapped-exec_command", "wrapped-write_stdin", "wrapped-wait"}
        tools = [tool for tool in tools if tool.name not in duplicated]
    tools.extend(modern_commands)
    tools.sort(key=lambda tool: tool.started)
    return tools, tokens, first, last


def _parse_claude(path: Path) -> tuple[list[ToolCall], dict[str, int], dt.datetime | None, dt.datetime | None]:
    pending: dict[str, tuple[str, str, dt.datetime]] = {}
    tools: list[ToolCall] = []
    totals: defaultdict[str, int] = defaultdict(int)
    first = last = None
    with path.open(errors="replace") as handle:
        for line in handle:
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            when = _time(row.get("timestamp"))
            if when:
                first = first or when
                last = when
            message = row.get("message") or {}
            usage = message.get("usage") or {}
            if row.get("type") == "assistant":
                for key, value in usage.items():
                    if isinstance(value, (int, float)):
                        totals[key] += int(value)
                for item in message.get("content") or []:
                    if not isinstance(item, dict) or item.get("type") != "tool_use" or not when:
                        continue
                    inp = item.get("input") or {}
                    command = str(inp.get("command") or inp.get("patch") or inp)
                    pending[str(item.get("id"))] = (str(item.get("name") or ""), command, when)
            elif row.get("type") == "user" and when:
                for item in message.get("content") or []:
                    if not isinstance(item, dict) or item.get("type") != "tool_result":
                        continue
                    tool_id = str(item.get("tool_use_id"))
                    if tool_id not in pending:
                        continue
                    name, command, started = pending.pop(tool_id)
                    seconds = max(0.0, (when - started).total_seconds())
                    tools.append(ToolCall(command, name, started, seconds, _category(command), _mutates(name, command)))
    return tools, dict(totals), first, last


def _transcript_index(state_dir: Path) -> dict[str, Path]:
    found = {}
    if not state_dir.is_dir():
        return found
    patterns = ("*/home/.codex/sessions/**/*.jsonl", "*/home/.claude/projects/**/*.jsonl")
    for pattern in patterns:
        for path in state_dir.glob(pattern):
            stem = path.stem
            session_id = stem.rsplit("-", 5)[-5:] if stem.startswith("rollout-") else None
            if session_id:
                candidate = "-".join(session_id)
                if re.fullmatch(r"[0-9a-f-]{36}", candidate):
                    found[candidate] = path
            elif re.fullmatch(r"[0-9a-f-]{36}", stem):
                found[stem] = path
    return found


def _initial_prompt(path: Path, provider: str) -> str:
    """Return the first real user prompt, excluding Claude's queue/attachment records."""
    collected = []
    try:
        with path.open(errors="replace") as handle:
            for line_no, line in enumerate(handle):
                if line_no > 200:
                    break
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                message = (row.get("message") if provider == "claude" else (row.get("payload") or {})) or {}
                if provider == "codex":
                    if message.get("type") != "message" or message.get("role") != "user":
                        continue
                elif row.get("type") != "user" or message.get("role") != "user":
                    continue
                content = message.get("content")
                if isinstance(content, str):
                    collected.append(content)
                if isinstance(content, list):
                    texts = [str(item.get("text", "")) for item in content
                             if isinstance(item, dict) and item.get("type") in {"input_text", "text"}]
                    if texts:
                        collected.extend(texts)
                joined = "\n".join(collected)
                if _phase(joined) != "other":
                    return joined
    except OSError:
        pass
    return "\n".join(collected)


def _roadmap_area(text: str) -> str | None:
    claim = CLAIM_RE.search(text)
    if claim:
        return claim.group(1)
    for pattern in (
        r"Work ONLY within the `([^`]+)` roadmap",
        r"designated roadmap.*?`([^`]+)`",
        r"TauCetiRoadmap/([^/`\s]+)/README\.md",
    ):
        match = re.search(pattern, text, re.S | re.I)
        if match:
            return match.group(1)
    return None


def _roadmap_size(state_dir: Path, worker: str, area: str | None) -> int | None:
    if not area or area.lower() in {"any", "auto", "none"}:
        return None
    choices = [
        state_dir / worker / "refs" / "roadmap" / "TauCetiRoadmap" / area / "README.md",
        state_dir / "default" / "refs" / "roadmap" / "TauCetiRoadmap" / area / "README.md",
    ]
    for path in choices:
        try:
            return path.stat().st_size
        except OSError:
            pass
    return None


def analyze(logs_dir: Path, state_dir: Path, limit: int = 250) -> dict:
    index = _transcript_index(state_dir)
    candidates = []
    for path in logs_dir.glob("*/agent-*.log"):
        try:
            with path.open(errors="replace") as handle:
                head = handle.read(262_144)
            stat = path.stat()
        except OSError:
            continue
        match = SESSION_RE.search(head)
        if not match:
            continue
        candidates.append((stat.st_mtime, path, head, match))
    candidates.sort(reverse=True, key=lambda row: row[0])
    if limit:
        candidates = candidates[:limit]

    sessions: list[Session] = []
    missing = 0
    for _, path, head, match in candidates:
        provider, session_id, model = match.groups()
        transcript = index.get(session_id)
        if transcript is None:
            missing += 1
            continue
        try:
            parsed = _parse_codex(transcript) if provider == "codex" else _parse_claude(transcript)
        except OSError:
            missing += 1
            continue
        tools, tokens, started, ended = parsed
        prompt = _initial_prompt(transcript, provider)
        phase = _phase(prompt or head)
        area = _roadmap_area(prompt or head) if phase == "preparation" else None
        if phase == "preparation" and (not area or area.lower() in {"any", "auto", "none"}):
            for tool in tools:
                claim = CLAIM_RE.search(tool.command)
                if claim:
                    area = claim.group(1)
                    break
        worker = path.parent.name
        claim_times = [t.started for t in tools if "claim.sh acquire" in t.command]
        edit_times = [t.started for t in tools if t.mutates]
        sessions.append(
            Session(
                provider, session_id, model, phase, path, transcript, started, ended, tools, tokens,
                area, _roadmap_size(state_dir, worker, area),
                (claim_times[0] - started).total_seconds() if started and claim_times else None,
                (edit_times[0] - started).total_seconds() if started and edit_times else None,
            )
        )

    groups = {}
    for (provider, phase), rows in _group(sessions, lambda s: (s.provider, s.phase)).items():
        lean_seconds = [sum(t.seconds for t in s.tools if t.category) for s in rows]
        categories = {}
        for category in ("cache-get", "lake-build", "axioms", "lean-check", "mixed-lean"):
            calls = [t.seconds for s in rows for t in s.tools if t.category == category]
            per_session = [sum(t.seconds for t in s.tools if t.category == category) for s in rows]
            if calls:
                categories[category] = {
                    "calls": distribution(calls),
                    "seconds_per_session": distribution(per_session),
                    "total_hours": sum(calls) / 3600,
                }
        groups[f"{provider}/{phase}"] = {
            "sessions": len(rows),
            "session_wall_minutes": distribution(
                (s.ended - s.started).total_seconds() / 60
                for s in rows if s.started and s.ended
            ),
            "lean_tool_seconds_per_session": distribution(lean_seconds),
            "tools": categories,
        }

    prep = [s for s in sessions if s.phase == "preparation"]
    orientation = {
        "sessions": len(prep),
        "to_claim_seconds": distribution(s.orientation_to_claim_seconds for s in prep if s.orientation_to_claim_seconds is not None),
        "to_first_edit_seconds": distribution(s.orientation_to_edit_seconds for s in prep if s.orientation_to_edit_seconds is not None),
        "roadmap_bytes": distribution(float(s.roadmap_bytes) for s in prep if s.roadmap_bytes is not None),
    }
    pairs = [(float(s.roadmap_bytes), s.orientation_to_claim_seconds) for s in prep
             if s.roadmap_bytes is not None and s.orientation_to_claim_seconds is not None]
    orientation["roadmap_bytes_vs_claim_spearman"] = _spearman(pairs)
    prior_pairs = []
    prior_bins: defaultdict[str, list[float]] = defaultdict(list)
    seen_areas: defaultdict[str, int] = defaultdict(int)
    for session in sorted(prep, key=lambda s: s.started or dt.datetime.min.replace(tzinfo=dt.timezone.utc)):
        if not session.roadmap_area:
            continue
        prior = seen_areas[session.roadmap_area]
        seen_areas[session.roadmap_area] += 1
        if session.orientation_to_claim_seconds is None:
            continue
        prior_pairs.append((float(prior), session.orientation_to_claim_seconds))
        label = "0" if prior == 0 else "1-4" if prior < 5 else "5+"
        prior_bins[label].append(session.orientation_to_claim_seconds)
    orientation["prior_local_sessions_in_area_vs_claim_spearman"] = _spearman(prior_pairs)
    orientation["claim_seconds_by_prior_local_sessions_in_area"] = {
        label: distribution(values) for label, values in sorted(prior_bins.items())
    }

    return {
        "schema": 1,
        "coverage": {
            "agent_logs_considered": len(candidates),
            "sessions_parsed": len(sessions),
            "missing_transcripts": missing,
            "limit": limit,
        },
        "groups": groups,
        "orientation": orientation,
    }


def _group(values, key):
    out = defaultdict(list)
    for value in values:
        out[key(value)].append(value)
    return out


def _ranks(values: list[float]) -> list[float]:
    order = sorted(range(len(values)), key=values.__getitem__)
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i + 1
        while j < len(order) and values[order[j]] == values[order[i]]:
            j += 1
        rank = (i + j - 1) / 2
        for k in order[i:j]:
            ranks[k] = rank
        i = j
    return ranks


def _spearman(pairs: list[tuple[float, float]]) -> dict[str, float | int | None]:
    if len(pairs) < 3:
        return {"n": len(pairs), "rho": None}
    xs, ys = map(list, zip(*pairs))
    rx, ry = _ranks(xs), _ranks(ys)
    mx, my = statistics.fmean(rx), statistics.fmean(ry)
    numerator = sum((x - mx) * (y - my) for x, y in zip(rx, ry))
    denominator = math.sqrt(sum((x - mx) ** 2 for x in rx) * sum((y - my) ** 2 for y in ry))
    return {"n": len(pairs), "rho": numerator / denominator if denominator else None}


def infrastructure_model(
    *,
    pr_builds: float,
    pr_runner_minutes: float,
    pr_runner_vcpus: float,
    main_runner_minutes: float,
    main_runner_vcpus: float,
    pr_runner_usd_minute: float,
    main_runner_usd_minute: float,
    cache_objects: int,
    cache_gib: float,
    retained_cache_gib: float,
    merged_prs_month: float | None = None,
    pr_component_shares: tuple[float, float, float] = (1.86 / 8.77, 5.44 / 8.77, 1.47 / 8.77),
    main_component_shares: tuple[float, float, float] = (3.42 / 14.07, 1.15 / 14.07, 9.50 / 14.07),
) -> dict:
    def normalized(values: tuple[float, float, float], label: str) -> tuple[float, float, float]:
        if len(values) != 3 or any(not math.isfinite(value) or value < 0 for value in values):
            raise ValueError(f"{label} must contain three non-negative finite values")
        total = sum(values)
        if total <= 0:
            raise ValueError(f"{label} may not be all zero")
        return tuple(value / total for value in values)

    pr_component_shares = normalized(pr_component_shares, "pr_component_shares")
    main_component_shares = normalized(main_component_shares, "main_component_shares")
    pr_minutes = pr_builds * pr_runner_minutes
    vcpu_hours = (pr_minutes * pr_runner_vcpus + main_runner_minutes * main_runner_vcpus) / 60
    fetch_cost = (cache_objects + 1) / 1_000_000 * 0.36
    fetches_per_pr = pr_builds + 1  # branch/merge-queue builds plus post-merge main CI
    cache_fetch = {
        "objects": cache_objects,
        "gib": cache_gib,
        "gross_class_b_usd": fetch_cost,
        "fetches_per_merged_pr": fetches_per_pr,
        "gross_class_b_usd_per_merged_pr": fetch_cost * fetches_per_pr,
        "gib_per_merged_pr": cache_gib * fetches_per_pr,
        "egress_usd": 0.0,
        "note": "gross Standard-tier requests before Cloudflare's monthly free allowance",
    }
    if merged_prs_month is not None:
        monthly_requests = (cache_objects + 1) * fetches_per_pr * merged_prs_month
        cache_fetch["monthly_at_merged_prs"] = merged_prs_month
        cache_fetch["monthly_class_b_requests"] = monthly_requests
        cache_fetch["monthly_gib"] = cache_gib * fetches_per_pr * merged_prs_month
        cache_fetch["monthly_class_b_usd_after_free_tier"] = max(0, monthly_requests - 10_000_000) / 1_000_000 * 0.36
    component_names = ("fixed", "touched", "repository")
    decomposition = {}
    for name, pr_share, main_share in zip(component_names, pr_component_shares, main_component_shares):
        branch_minutes = pr_minutes * pr_share
        postmerge_minutes = main_runner_minutes * main_share
        decomposition[name] = {
            "runner_minutes_per_merged_pr": branch_minutes + postmerge_minutes,
            "equivalent_usd_per_merged_pr": (
                branch_minutes * pr_runner_usd_minute
                + postmerge_minutes * main_runner_usd_minute
            ),
            "pr_build_share": pr_share,
            "postmerge_share": main_share,
        }
    retained_cache_gb = retained_cache_gib * (2 ** 30 / 1e9)
    return {
        "ci": {
            "branch_and_merge_queue_builds_per_merged_pr": pr_builds,
            "runner_minutes_per_merged_pr": pr_minutes + main_runner_minutes,
            "vcpu_hours_per_merged_pr": vcpu_hours,
            "equivalent_usd_per_merged_pr": (
                pr_minutes * pr_runner_usd_minute + main_runner_minutes * main_runner_usd_minute
            ),
            "decomposition": decomposition,
            "decomposition_note": (
                "proxy classification, not a causal fit: fixed/setup, candidate build work, and "
                "whole-repository checks from 40 PR jobs and 20 post-merge jobs sampled 2026-09-18; "
                "a 97-run cross-check found wall-time Pearson correlations of 0.06 with changed LOC "
                "and 0.04 with changed-file count"
            ),
        },
        "r2_complete_cache_fetch": cache_fetch,
        "r2_complete_cache_publish": {
            "gross_class_a_usd": (cache_objects + 1) / 1_000_000 * 4.50,
            "note": "upper bound if every archive is newly written; content addressing normally deduplicates most objects",
        },
        "r2_storage": {
            "estimated_retained_gib": retained_cache_gib,
            "estimated_retained_gb": retained_cache_gb,
            "gross_usd_month": retained_cache_gb * 0.015,
            "usd_month_after_10_gb_free_tier": max(0.0, retained_cache_gb - 10) * 0.015,
            "note": "estimate from locally retained TauCeti mappings and current archive-size sample",
        },
    }


def loc_cost_model(
    infrastructure: dict,
    *,
    ai_usd_per_changed_loc: float,
    changed_loc_per_pr: float,
    changed_per_net_loc: float,
    reference_repo_loc: float,
    projection_locs: Iterable[float],
) -> dict:
    """Turn the per-PR infrastructure estimate into an explicit LOC scaling scenario.

    Fixed and touched-code CI are held constant per changed LOC. Only checks classified as
    repository-wide, plus a complete R2 cache read, scale linearly with existing repository LOC.
    This is intentionally a scenario rather than a fitted forecast: the available historical CI
    series contains operational changes large enough to swamp the repository-size signal.
    """
    decomposition = infrastructure["ci"]["decomposition"]
    per_changed = {
        name: values["equivalent_usd_per_merged_pr"] / changed_loc_per_pr
        for name, values in decomposition.items()
    }
    cache_per_changed = (
        infrastructure["r2_complete_cache_fetch"]["gross_class_b_usd_per_merged_pr"]
        / changed_loc_per_pr
    )
    repository_at_reference = per_changed["repository"] + cache_per_changed
    intercept = ai_usd_per_changed_loc + per_changed["fixed"] + per_changed["touched"]
    slope = repository_at_reference / reference_repo_loc
    integrated_quadratic = slope / 2

    # A deliberately pessimistic comparison with the earlier simple model, in which every dollar
    # of present-day CI and cache cost grows with repository size.
    all_infra_at_reference = sum(per_changed.values()) + cache_per_changed
    upper_slope = all_infra_at_reference / reference_repo_loc

    projections = []
    for loc in projection_locs:
        changed_marginal = intercept + slope * loc
        no_churn_cumulative = intercept * loc + integrated_quadratic * loc * loc
        projections.append({
            "repository_loc": loc,
            "changed_loc": {
                "marginal_usd": changed_marginal,
                "no_churn_cumulative_usd": no_churn_cumulative,
            },
            "net_retained_loc": {
                "marginal_usd": changed_marginal * changed_per_net_loc,
                "cumulative_usd": no_churn_cumulative * changed_per_net_loc,
            },
        })

    return {
        "reference_repo_loc": reference_repo_loc,
        "changed_loc_per_pr": changed_loc_per_pr,
        "changed_per_net_loc": changed_per_net_loc,
        "current_usd_per_changed_loc": {
            "ai_preparation_review_revision": ai_usd_per_changed_loc,
            "ci_fixed": per_changed["fixed"],
            "ci_touched": per_changed["touched"],
            "ci_repository": per_changed["repository"],
            "r2_repository": cache_per_changed,
            "total": intercept + slope * reference_repo_loc,
        },
        "best_scaling_scenario": {
            "marginal_intercept_usd": intercept,
            "marginal_slope_usd_per_existing_loc": slope,
            "integrated_linear_usd": intercept,
            "integrated_quadratic_usd": integrated_quadratic,
            "retained_integrated_linear_usd": intercept * changed_per_net_loc,
            "retained_integrated_quadratic_usd": integrated_quadratic * changed_per_net_loc,
            "repository_usd_per_changed_loc_at_reference": repository_at_reference,
            "note": (
                "fixed and touched-code CI stay constant per changed LOC; repository-wide CI "
                "and R2 reads grow linearly with existing LOC"
            ),
        },
        "all_infrastructure_scales_scenario": {
            "marginal_intercept_usd": ai_usd_per_changed_loc,
            "marginal_slope_usd_per_existing_loc": upper_slope,
            "integrated_linear_usd": ai_usd_per_changed_loc,
            "integrated_quadratic_usd": upper_slope / 2,
            "note": "pessimistic comparison in which all present-day CI and R2 cost scales with repository LOC",
        },
        "projections": projections,
        "caveat": (
            "scaling scenario, not a forecast; CI architecture, cache behavior, PR size, model "
            "prices, and agent behavior are held fixed"
        ),
    }


def format_report(report: dict) -> str:
    lines = []
    coverage = report["coverage"]
    lines.append(
        f"local sample: {coverage['sessions_parsed']}/{coverage['agent_logs_considered']} sessions parsed"
        + (f" ({coverage['missing_transcripts']} transcript(s) missing)" if coverage["missing_transcripts"] else "")
    )
    lines.append("")
    lines.append("local Lean/cache wall time per session:")
    for name, group in sorted(report["groups"].items()):
        dist = group["lean_tool_seconds_per_session"]
        if dist["n"]:
            lines.append(
                f"  {name}: n={dist['n']}, median={dist['median']:.1f}s, "
                f"p90={dist['p90']:.1f}s, mean={dist['mean']:.1f}s"
            )
    orient = report["orientation"]
    lines.append("")
    lines.append("author orientation (preparation sessions):")
    for label, key in (("to target claim", "to_claim_seconds"), ("to first edit", "to_first_edit_seconds")):
        dist = orient[key]
        if dist["n"]:
            lines.append(
                f"  {label}: n={dist['n']}, median={dist['median'] / 60:.1f}m, "
                f"p90={dist['p90'] / 60:.1f}m, mean={dist['mean'] / 60:.1f}m"
            )
    corr = orient["roadmap_bytes_vs_claim_spearman"]
    if corr["rho"] is not None:
        lines.append(f"  roadmap bytes vs claim time: Spearman rho={corr['rho']:.2f} (n={corr['n']}; exploratory)")
    corr = orient["prior_local_sessions_in_area_vs_claim_spearman"]
    if corr["rho"] is not None:
        lines.append(
            f"  prior local sessions in area vs claim time: Spearman rho={corr['rho']:.2f} "
            f"(n={corr['n']}; exploratory)"
        )
    infra = report["infrastructure"]
    lines.append("")
    lines.append(
        "CI model: "
        f"{infra['ci']['runner_minutes_per_merged_pr']:.1f} runner-min/merged PR, "
        f"{infra['ci']['vcpu_hours_per_merged_pr']:.2f} vCPU-h, "
        f"${infra['ci']['equivalent_usd_per_merged_pr']:.2f} equivalent compute"
    )
    decomposition = infra["ci"]["decomposition"]
    lines.append(
        "  proxy decomposition: "
        f"fixed={decomposition['fixed']['runner_minutes_per_merged_pr']:.1f}, "
        f"touched-code={decomposition['touched']['runner_minutes_per_merged_pr']:.1f}, "
        f"repository-wide={decomposition['repository']['runner_minutes_per_merged_pr']:.1f} runner-min/merged PR"
    )
    cache = infra["r2_complete_cache_fetch"]
    lines.append(
        f"R2 full cache fetch: {cache['objects']:,} objects, {cache['gib']:.3f} GiB, "
        f"${cache['gross_class_b_usd']:.4f} gross reads, $0 R2 egress"
    )
    lines.append(
        f"  CI multiplier: {cache['fetches_per_merged_pr']:.2f} fetches/merged PR = "
        f"{cache['gib_per_merged_pr']:.3f} GiB and ${cache['gross_class_b_usd_per_merged_pr']:.4f} gross reads"
    )
    if "monthly_at_merged_prs" in cache:
        lines.append(
            f"  at {cache['monthly_at_merged_prs']:g} merged PRs/month: "
            f"{cache['monthly_gib'] / 1024:.2f} TiB, "
            f"${cache['monthly_class_b_usd_after_free_tier']:.2f}/month reads after the free tier"
        )
    storage = infra["r2_storage"]
    lines.append(
        f"R2 retained storage estimate: {storage['estimated_retained_gib']:.2f} GiB, "
        f"${storage['gross_usd_month']:.3f}/month gross, "
        f"${storage['usd_month_after_10_gb_free_tier']:.2f}/month after the Standard free tier"
    )
    loc = report.get("loc_cost")
    if loc:
        components = loc["current_usd_per_changed_loc"]
        lines.append("")
        lines.append("LOC cost at the reference repository size:")
        lines.append(
            f"  N={loc['reference_repo_loc']:.3e}: ${components['total']:.3e}/changed LOC "
            f"(${components['total'] * loc['changed_per_net_loc']:.3e}/net retained LOC)"
        )
        lines.append(
            "  changed-LOC split: "
            f"AI=${components['ai_preparation_review_revision']:.3e}, "
            f"fixed CI=${components['ci_fixed']:.3e}, "
            f"touched-code CI=${components['ci_touched']:.3e}, "
            f"repository CI+R2=${components['ci_repository'] + components['r2_repository']:.3e}"
        )
        scaling = loc["best_scaling_scenario"]
        lines.append("")
        lines.append("LOC scaling scenario (N = existing repository LOC):")
        lines.append(
            f"  C(N) = ${scaling['marginal_intercept_usd']:.3e} + "
            f"${scaling['marginal_slope_usd_per_existing_loc']:.3e} N"
        )
        lines.append(
            f"  T_retained(N) = ${scaling['retained_integrated_linear_usd']:.3e} N + "
            f"${scaling['retained_integrated_quadratic_usd']:.3e} N^2"
        )
        lines.append("  target retained LOC    no-churn baseline    retained-growth total")
        for projection in loc["projections"]:
            lines.append(
                f"  {projection['repository_loc']:.3e}    "
                f"${projection['changed_loc']['no_churn_cumulative_usd']:.3e}             "
                f"${projection['net_retained_loc']['cumulative_usd']:.3e}"
            )
        upper = loc["all_infrastructure_scales_scenario"]
        lines.append(
            "  all-infrastructure-scales comparison: "
            f"C(N) = ${upper['marginal_intercept_usd']:.3e} + "
            f"${upper['marginal_slope_usd_per_existing_loc']:.3e} N"
        )
        lines.append(f"  caveat: {loc['caveat']}")
    return "\n".join(lines)

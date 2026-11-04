#!/usr/bin/env python3
"""Staging journal report: the staging triage measurements from the Bors candidate journal.

Usage::

    uv run python scripts/staging_journal_report.py --journal candidates.json [--journal later.json ...] \\
        [--url https://bors.taucetiproject.org/repositories/1/candidates?base=main] \\
        [--from-candidate 1833] [--to-candidate 1932] [--since 2026-11-02T23:00Z] [--until 2026-11-03T23:00Z] \\
        [--markdown report.md] [--json report.json]

    uv run python scripts/staging_journal_report.py --reduce --journal candidates.json \\
        --candidates 1886,1890,1928 --output tests/fixtures/staging-journal-fixture.json

Inputs are snapshots of the feed ``/repositories/<n>/candidates?base=main`` (schema
``tauceti-bors.candidates/v1``, timestamps in unix microseconds, the 100 most recent candidates and
the 500 most recent admissions). Several snapshots are merged by candidate id; when a candidate
appears in more than one, the copy from the snapshot observed later wins (a snapshot's observation
time is the latest ``observed_at`` it contains), so a candidate that was still running in an early
snapshot takes its outcome from the later one. Admissions are merged by id the same way.

The window (``--from-candidate``/``--to-candidate`` on candidate ids, ``--since``/``--until`` on the
dispatch time, or the candidate's ``observed_at`` when it was never dispatched) selects the
candidates that are measured. Readmission of handed-off suspects is searched in every loaded
candidate and admission, inside or outside the window, because the readmission follows the handoff.

Classification rules
====================

Run
    A candidate is a run (it occupied the staging slot) when its journal has a ``dispatched``
    event (``github.workflow_dispatch_response``), or failing that a ``dispatch_requested`` event
    (``bors.outbox``). Candidates without either are non-runs and are counted by reason:
    construction conflict (``local_conflict``), construction import failure
    (``local_import_failure``), construction stopped for another reason, or superseded before
    dispatch (``train_released`` with reason ``never_dispatched``, the usual case for a batch
    generation replaced by a later generation of the same batch).

Slot minutes
    From ``runner_started_at`` to ``runner_finished_at`` when the feed supplies them. The current
    feed never does (``unknown_reason``: "runner lifecycle is not supplied by commit statuses"), so
    the fallback is the dispatch event to the earliest terminal event: ``terminal_observed``
    (``bors.status_summary``), ``promoted`` (``github.base_push_response``), ``train_invalidated``
    (``bors.train``) or ``cancellation_terminal`` (``bors.cancellation``). When none of those was
    journaled but ``bors.trusted_failure failure_classified`` was, the last such classification
    is the end (timing source ``dispatch_to_failure_classified``). Each run records the timing
    source it used. The dispatch event follows ``staging_pushed`` within a second and the GitHub
    run is created a few seconds later, so this is the slot's occupancy, queueing included.

Outcome
    The earliest terminal event decides: ``promoted`` (a ``promoted`` event; the end time is the
    green ``terminal_observed`` when present), ``failed`` (``terminal_observed`` with state
    ``error`` or ``failure``, or a trusted ``failure_classified`` with no terminal event),
    ``cancelled`` (``cancellation_terminal``), ``invalidated`` (``train_invalidated``, the
    speculative train's predecessor failed or changed). A dispatched candidate with none of these
    is unclassified with the reason "dispatched, no outcome in journal", as is a green terminal
    without promotion.

Size class
    ``large`` is 15 or more members, ``small`` is 2 to 14 members (the triage spec's "small batch
    (at most 14)"), ``singleton`` is one member.

Singleton kind
    From the single member's ``source`` and ``retry_reason``: ``admitted_alone`` for
    ``bors.admission`` (a PR approved with nothing else waiting); ``conflict`` for the retry
    reasons ``structured_conflict``, ``conflict_isolation`` and ``merge_conflict_deferred`` (the
    conflicting PR of a construction conflict placed in its own retry batch; the parent's
    ``local_conflict`` event is checked to name this PR and the check's result is recorded);
    ``confirmation`` for a located suspect retried alone (``compiler_diagnostics``,
    ``bounded_provider_hint``, ``import_provider_hint``, ``recovered_parent_diagnostics``,
    ``missing_import``); ``remainder`` for ``provisional_remainder``, ``diagnostic_remainder`` and
    ``member_removed``; ``bisection`` for ``bisection``, ``dependency_component`` and
    ``balanced_dependency_components`` halves of one. Neutral retries (``train_invalidated``,
    ``stale_dispatch``, ``cancellation``, ``non_fast_forward``, ``provisional_unconfirmed``)
    inherit the kind of the parent candidate when it is loaded, since they rerun the same batch;
    a neutral retry of a larger parent is a remainder; otherwise the kind is ``unknown`` with the
    reason recorded.

Slot occupancy categories
    Large batch failed; large batch promoted; large batch cancelled or invalidated; singleton
    promoted; singleton failed, cancelled or invalidated; small batch, any outcome; dispatched
    without an outcome in the journal (runs with no measurable minutes). The triage spec's table
    has no row for cancelled or invalidated large batches; see docs/queue/journal-report.md for
    how its rows map onto these.

Lineages
    A candidate's parent is the ``parent_candidate_id`` most of its members carry. A lineage is
    the tree below a candidate whose parent is absent or outside the selected window. A candidate
    is superseded when the window holds a later generation of the same batch. A lineage is
    complete when every leaf is promoted, superseded, quarantined (``head_quarantined``) or a
    handoff with no remainder; otherwise the leaf's retry is still waiting or outside the window.
    Failed rounds are the lineage's runs with outcome ``failed`` whatever their size, which is how
    the smaller descendants of a split are counted.

Failed rounds per landed large batch
    Window aggregate: failed large runs divided by promoted large runs. Per completed lineage:
    the lineage's failed runs divided by its promoted runs, and the same ratio summed over all
    complete lineages whose root is large.

Bisections and unlocated splits
    A deterministic bisection is a ``failure_partition`` with strategy ``deterministic_bisection``
    and at least two groups. The same strategy with no groups is an indivisible failure: the
    single PR is quarantined, not bisected. ``balanced_dependency_components`` and
    ``dependency_component`` partitions are unlocated splits by import connectivity and are
    counted beside bisections. For each, the report lists the candidate's ``failure_diagnostics``
    observations (kind, paths, linter, declaration, the ``unknown`` marker) and ``raw_result`` as
    the lines the parser left unattributed; when the parser recorded no observation at all the
    report says so, since that is the gap the triage spec's change 4 targets.

Suspects per partition
    For each ``failure_partition``: the PRs of the ``provisional_repair`` that follows it in the
    same candidate (``suspects``, else ``prs``, else the single ``pr``), otherwise the first group
    of a located strategy (``structured_conflict``, ``compiler_diagnostics``,
    ``bounded_provider_hint``, ``import_provider_hint``, ``recovered_parent_diagnostics``,
    ``missing_import``; the partition code writes the suspects as the first group), otherwise
    zero for the unlocated strategies.

Handoff to readmission
    Each ``provisional_repair`` names its suspects and the head they were withdrawn at. The
    suspect's readmission is the earliest later appearance of the PR at a different head, either
    as a member of a later candidate (time: the member's ``original_admitted_at`` when it is after
    the handoff, else the candidate's ``observed_at``) or as a ``bors.admission`` record in the
    feed's admissions list. Suspects with no such appearance are still withdrawn as of the latest
    observation in the loaded data, and are listed by PR.

Unclassified
    Every candidate the rules above could not place is listed with its reason, so a gap in the
    journal or in these rules is visible rather than silent.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import urllib.request
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path

SCHEMA = "tauceti.staging-journal-report/v1"
FEED_SCHEMA = "tauceti-bors.candidates/v1"
LARGE = 15

CONFLICT_REASONS = {"structured_conflict", "conflict_isolation", "merge_conflict_deferred"}
SUSPECT_REASONS = {
    "compiler_diagnostics",
    "bounded_provider_hint",
    "import_provider_hint",
    "recovered_parent_diagnostics",
    "missing_import",
}
REMAINDER_REASONS = {"provisional_remainder", "diagnostic_remainder", "member_removed"}
BISECTION_REASONS = {"bisection", "dependency_component", "balanced_dependency_components"}
NEUTRAL_REASONS = {"train_invalidated", "stale_dispatch", "cancellation", "non_fast_forward", "provisional_unconfirmed"}
LOCATED_STRATEGIES = {
    "structured_conflict",
    "compiler_diagnostics",
    "bounded_provider_hint",
    "import_provider_hint",
    "recovered_parent_diagnostics",
    "missing_import",
}
COMPONENT_STRATEGIES = {"balanced_dependency_components", "dependency_component"}

OCCUPANCY_ROWS = [
    ("large_failed", "Large batch, failed"),
    ("large_promoted", "Large batch, promoted"),
    ("large_cancelled_or_invalidated", "Large batch, cancelled or invalidated"),
    ("singleton_promoted", "Singleton, promoted"),
    ("singleton_failed_or_invalidated", "Singleton, failed, cancelled or invalidated"),
    ("small_any", "Small batch (2 to 14), any outcome"),
    ("no_outcome", "Dispatched, no outcome in journal"),
]
SINGLETON_KINDS = ["conflict", "confirmation", "remainder", "bisection", "admitted_alone", "unknown"]

# Event detail fields the report reads, by event kind; the fixture reducer keeps exactly these.
DETAIL_FIELDS = {
    "failure_partition": ["strategy", "groups", "reason"],
    "provisional_repair": ["pr", "prs", "suspects", "head_sha", "conflict", "remainder"],
    "failure_diagnostics": ["result", "raw_result"],
    "failure_classified": ["classification"],
    "local_conflict": ["pr", "paths", "kind"],
    "local_import_failure": ["missing_imports"],
    "head_quarantined": ["pr"],
    "terminal_observed": ["state"],
    "promoted": ["commit", "base_sha"],
    "train_invalidated": ["reason"],
    "cancellation_terminal": ["outcome"],
    "train_released": ["reason"],
    "construction_stopped": ["state"],
    "dispatched": ["commit"],
    "dispatch_requested": ["commit"],
}
MEMBER_FIELDS = ["pr", "head_sha", "source", "retry_reason", "parent_candidate_id", "original_admitted_at"]
# Event kinds the report never reads; the reducer drops them (status polls dominate the feed's size).
DROPPED_EVENT_KINDS = {
    "status_observed",
    "source_compatibility_checked",
    "local_assembled",
    "train_barrier_checked",
    "merge_selection",
    "constructed",
    "construction_started",
    "local_assembly_selected",
    "train_reserved",
    "successor_reserved",
    "diagnostic_alert_delivered",
    "staging_pushed",
    "promotion_requested",
    "cancellation_run_bound",
    "cancellation_requested",
    "diagnostic_degraded",
    "diagnostic_recovered",
}


# --- time helpers ---------------------------------------------------------------------------------


def iso(us):
    if us is None:
        return None
    return datetime.fromtimestamp(us / 1e6, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def day(us):
    return datetime.fromtimestamp(us / 1e6, UTC).strftime("%Y-%m-%d")


def minutes(start, end):
    if start is None or end is None:
        return None
    return round((end - start) / 6e7, 1)


def parse_utc(text):
    """Parse a UTC timestamp argument into unix microseconds."""
    value = text.strip()
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return int(parsed.timestamp() * 1e6)


def distribution(values):
    ordered = sorted(v for v in values if v is not None)
    if not ordered:
        return {"count": 0}
    return {
        "count": len(ordered),
        "sum": round(sum(ordered), 1),
        "median": round(statistics.median(ordered), 1),
        "min": ordered[0],
        "max": ordered[-1],
    }


# --- loading and merging --------------------------------------------------------------------------


def load_journal(path):
    return json.loads(Path(path).read_text())


def fetch_journal(url):
    with urllib.request.urlopen(url, timeout=120) as response:
        return json.load(response)


def snapshot_time(snapshot):
    """The latest observation a snapshot contains; later snapshots win when merging."""
    times = [c.get("observed_at") or 0 for c in snapshot.get("candidates", [])]
    times += [e["observed_at"] for c in snapshot.get("candidates", []) for e in c.get("events", [])]
    times += [a["observed_at"] for a in snapshot.get("admissions") or []]
    return max(times, default=0)


def merge_snapshots(snapshots, labels=None):
    """Merge feed snapshots by candidate id, preferring the snapshot observed later."""
    labels = labels or [f"snapshot {i}" for i in range(len(snapshots))]
    for label, snap in zip(labels, snapshots, strict=True):
        if snap.get("schema") != FEED_SCHEMA:
            raise SystemExit(f"{label}: unexpected schema {snap.get('schema')!r}, expected {FEED_SCHEMA}")
        if snap.get("timestamp_unit", "unix_microseconds") != "unix_microseconds":
            raise SystemExit(f"{label}: unexpected timestamp_unit {snap.get('timestamp_unit')!r}")
    ordered = sorted(range(len(snapshots)), key=lambda i: (snapshot_time(snapshots[i]), i))
    candidates, admissions, origin = {}, {}, {}
    for i in ordered:
        for cand in snapshots[i].get("candidates", []):
            candidates[cand["id"]] = cand
            origin[cand["id"]] = labels[i]
        for adm in snapshots[i].get("admissions") or []:
            admissions[adm["id"]] = adm
    latest = snapshots[ordered[-1]] if ordered else {}
    return {
        "candidates": candidates,
        "admissions": admissions,
        "origin": origin,
        "horizon": max((snapshot_time(s) for s in snapshots), default=None),
        "repo": latest.get("repo"),
        "base": latest.get("base"),
        "inputs": [{"label": labels[i], "observed_through": iso(snapshot_time(snapshots[i]))} for i in ordered],
    }


# --- per-candidate classification -----------------------------------------------------------------


def events(cand, kind, source=None):
    return [e for e in cand.get("events", []) if e["kind"] == kind and (source is None or e["source"] == source)]


def first_time(cand, kind, source=None):
    found = events(cand, kind, source)
    return min(e["observed_at"] for e in found) if found else None


def members(cand):
    return cand.get("snapshot", {}).get("members") or []


def size_class(size):
    if size >= LARGE:
        return "large"
    return "small" if size >= 2 else "singleton"


def parent_id(cand):
    parents = Counter(m.get("parent_candidate_id") for m in members(cand) if m.get("parent_candidate_id") is not None)
    return parents.most_common(1)[0][0] if parents else None


def describe(cand):
    """Run, timing and outcome for one candidate."""
    size = len(members(cand))
    info = {
        "id": cand["id"],
        "batch_id": cand.get("batch_id"),
        "generation": cand.get("generation"),
        "size": size,
        "size_class": size_class(size),
        "observed_at": cand.get("observed_at"),
        "parent_candidate_id": parent_id(cand),
        "dispatched_at": None,
        "ended_at": None,
        "minutes": None,
        "timing_source": None,
        "outcome": None,
        "run": False,
        "non_run_reason": None,
        "unclassified_reason": None,
    }
    dispatched = first_time(cand, "dispatched") or first_time(cand, "dispatch_requested")
    if dispatched is None:
        stopped = events(cand, "construction_stopped")
        released = [e for e in events(cand, "train_released") if e["details"].get("reason") == "never_dispatched"]
        if events(cand, "local_conflict"):
            info["non_run_reason"] = "construction conflict"
        elif events(cand, "local_import_failure"):
            info["non_run_reason"] = "construction import failure"
        elif stopped:
            info["non_run_reason"] = f"construction stopped ({stopped[0]['details'].get('state')})"
        elif released:
            info["non_run_reason"] = "superseded before dispatch"
        else:
            info["unclassified_reason"] = "no dispatch and no construction record"
        return info

    info["run"] = True
    info["dispatched_at"] = dispatched
    ends = []
    promoted = first_time(cand, "promoted")
    green = [e for e in events(cand, "terminal_observed") if e["details"].get("state") in ("ok", "success")]
    red = [e for e in events(cand, "terminal_observed") if e["details"].get("state") in ("error", "failure")]
    if promoted is not None:
        ends.append((min([promoted] + [e["observed_at"] for e in green]), "promoted"))
    if red:
        ends.append((min(e["observed_at"] for e in red), "failed"))
    cancelled = first_time(cand, "cancellation_terminal")
    if cancelled is not None:
        ends.append((cancelled, "cancelled"))
    invalidated = first_time(cand, "train_invalidated")
    if invalidated is not None:
        ends.append((invalidated, "invalidated"))
    timing = "dispatch_to_terminal"
    if not ends:
        classified = [
            e for e in events(cand, "failure_classified", "bors.trusted_failure") if e["observed_at"] >= dispatched
        ]
        if classified:
            ends.append((max(e["observed_at"] for e in classified), "failed"))
            timing = "dispatch_to_failure_classified"
        elif green:
            info["unclassified_reason"] = "green terminal without promotion"
        else:
            info["unclassified_reason"] = "dispatched, no outcome in journal"
    if ends:
        end, outcome = min(ends)
        info.update(ended_at=end, outcome=outcome, minutes=minutes(dispatched, end), timing_source=timing)
    if cand.get("runner_started_at") and cand.get("runner_finished_at"):
        info.update(
            dispatched_at=cand["runner_started_at"],
            ended_at=cand["runner_finished_at"],
            minutes=minutes(cand["runner_started_at"], cand["runner_finished_at"]),
            timing_source="runner_lifecycle",
        )
    return info


def singleton_kind(cand, candidates, depth=0):
    """Kind and evidence for a one-member candidate (see the module docstring)."""
    member = members(cand)[0]
    source, reason = member.get("source"), member.get("retry_reason")
    parent = candidates.get(member.get("parent_candidate_id"))
    if source == "bors.admission" or (source is None and reason is None):
        return "admitted_alone", "bors.admission"
    if reason in CONFLICT_REASONS:
        if parent is None:
            evidence = f"{reason}; parent candidate {member.get('parent_candidate_id')} not in the journal"
        elif any(e["details"].get("pr") == member["pr"] for e in events(parent, "local_conflict")):
            evidence = f"{reason}; parent {parent['id']} local_conflict names PR {member['pr']}"
        else:
            evidence = f"{reason}; parent {parent['id']} has no local_conflict naming PR {member['pr']}"
        return "conflict", evidence
    if reason in SUSPECT_REASONS:
        return "confirmation", f"{reason} suspect retried alone"
    if reason in REMAINDER_REASONS:
        return "remainder", reason
    if reason in BISECTION_REASONS:
        return "bisection", reason
    if reason in NEUTRAL_REASONS:
        if parent is None:
            return "unknown", f"{reason} retry; parent candidate {member.get('parent_candidate_id')} not in the journal"
        if len(members(parent)) == 1 and depth < 16:
            kind, evidence = singleton_kind(parent, candidates, depth + 1)
            return kind, f"{reason} retry of candidate {parent['id']}: {evidence}"
        return "remainder", f"{reason} retry of candidate {parent['id']} ({len(members(parent))} members)"
    return "unknown", f"unrecognised retry_reason {reason!r} (source {source!r})"


def occupancy_row(info):
    if info["outcome"] is None:
        return "no_outcome"
    cls, outcome = info["size_class"], info["outcome"]
    if cls == "small":
        return "small_any"
    if cls == "large":
        if outcome == "promoted":
            return "large_promoted"
        return "large_failed" if outcome == "failed" else "large_cancelled_or_invalidated"
    return "singleton_promoted" if outcome == "promoted" else "singleton_failed_or_invalidated"


def repair_suspects(details):
    """Suspects named by a provisional_repair event, in both the single-pr and the set form."""
    if details.get("suspects"):
        return [{"pr": s["pr"], "head_sha": s.get("head_sha")} for s in details["suspects"]]
    if details.get("prs"):
        head = details.get("head_sha")
        return [{"pr": pr, "head_sha": head if pr == details.get("pr") else None} for pr in details["prs"]]
    if details.get("pr") is not None:
        return [{"pr": details["pr"], "head_sha": details.get("head_sha")}]
    return []


# --- window and analysis --------------------------------------------------------------------------


def select_window(candidates, from_id=None, to_id=None, since=None, until=None):
    chosen = {}
    for cid, cand in candidates.items():
        if from_id is not None and cid < from_id:
            continue
        if to_id is not None and cid > to_id:
            continue
        when = first_time(cand, "dispatched") or first_time(cand, "dispatch_requested") or cand.get("observed_at")
        if since is not None and (when is None or when < since):
            continue
        if until is not None and (when is None or when >= until):
            continue
        chosen[cid] = cand
    return chosen


def lineages(window, infos):
    children = defaultdict(list)
    roots = []
    for cid in sorted(window):
        parent = infos[cid]["parent_candidate_id"]
        if parent is None or parent not in window:
            roots.append(cid)
        else:
            children[parent].append(cid)
    latest_generation = {}
    for cand in window.values():
        key = cand.get("batch_id")
        latest_generation[key] = max(latest_generation.get(key, 0), cand.get("generation") or 0)

    def superseded(cid):
        cand = window[cid]
        return (cand.get("generation") or 0) < latest_generation.get(cand.get("batch_id"), 0)

    result = []
    for root in roots:
        ids, stack = [], [root]
        while stack:
            cid = stack.pop()
            ids.append(cid)
            stack.extend(sorted(children[cid], reverse=True))
        ids.sort()
        runs = [infos[c] for c in ids if infos[c]["run"]]
        promoted = [r for r in runs if r["outcome"] == "promoted"]
        failed = [r for r in runs if r["outcome"] == "failed"]
        handoffs, quarantined, open_leaves = [], [], []
        for cid in ids:
            cand = window[cid]
            for e in events(cand, "provisional_repair"):
                handoffs.extend(s["pr"] for s in repair_suspects(e["details"]))
            quarantined.extend(e["details"]["pr"] for e in events(cand, "head_quarantined"))
            if children[cid]:
                continue
            info = infos[cid]
            fully_handed_off = any(
                not any(e["details"].get("remainder") or []) for e in events(cand, "provisional_repair")
            )
            if info["outcome"] == "promoted" or superseded(cid) or events(cand, "head_quarantined") or fully_handed_off:
                continue
            open_leaves.append(cid)
        parent = infos[root]["parent_candidate_id"]
        result.append(
            {
                "root": root,
                "root_size": infos[root]["size"],
                "root_parent_outside_window": parent,
                "candidates": ids,
                "runs": len(runs),
                "failed_rounds": len(failed),
                "cancelled_or_invalidated": sum(r["outcome"] in ("cancelled", "invalidated") for r in runs),
                "promoted_runs": [{"id": r["id"], "size": r["size"]} for r in promoted],
                "landed_prs": sum(r["size"] for r in promoted),
                "handed_off_prs": sorted(set(handoffs)),
                "quarantined_prs": sorted(set(quarantined)),
                "open_leaves": open_leaves,
                "complete": not open_leaves,
                "failed_per_promoted": round(len(failed) / len(promoted), 2) if promoted else None,
            }
        )
    return result


def diagnostics_lines(cand):
    """What the diagnostics parser recorded for a candidate: observations and any raw remainder."""
    lines = []
    for e in events(cand, "failure_diagnostics"):
        result = e["details"].get("result") or {}
        for job in result.get("jobs") or []:
            for obs in job.get("observations") or []:
                lines.append({k: obs[k] for k in ("kind", "paths", "linter", "declaration", "unknown") if k in obs})
        raw = e["details"].get("raw_result")
        if raw:
            lines.append({"raw_result": raw})
    unique = []
    for line in lines:
        if line not in unique:
            unique.append(line)
    return unique


def analyse(merged, window):
    candidates = merged["candidates"]
    infos = {cid: describe(cand) for cid, cand in window.items()}
    for cid, info in infos.items():
        if info["size_class"] == "singleton":
            info["singleton_kind"], info["singleton_evidence"] = singleton_kind(window[cid], candidates)
        if info["run"]:
            info["occupancy_row"] = occupancy_row(info)

    runs = [i for i in infos.values() if i["run"]]
    occupancy = {}
    for key, _label in OCCUPANCY_ROWS:
        rows = [r for r in runs if r["occupancy_row"] == key]
        occupancy[key] = {
            "runs": len(rows),
            "slot_minutes": round(sum(r["minutes"] or 0 for r in rows), 1),
            "candidates": sorted(r["id"] for r in rows),
        }
    total_minutes = round(sum(r["minutes"] or 0 for r in runs), 1)
    for row in occupancy.values():
        row["share"] = round(row["slot_minutes"] / total_minutes, 3) if total_minutes else None

    singleton_runs = [r for r in runs if r["size_class"] == "singleton"]
    singleton_table = {}
    for kind in SINGLETON_KINDS:
        rows = [r for r in singleton_runs if r["singleton_kind"] == kind]
        singleton_table[kind] = {
            "runs": len(rows),
            "slot_minutes": round(sum(r["minutes"] or 0 for r in rows), 1),
            "promoted": sum(r["outcome"] == "promoted" for r in rows),
            "failed": sum(r["outcome"] == "failed" for r in rows),
            "cancelled_or_invalidated": sum(r["outcome"] in ("cancelled", "invalidated") for r in rows),
            "candidates": sorted(r["id"] for r in rows),
        }
    singletons_per_day = defaultdict(Counter)
    for r in singleton_runs:
        singletons_per_day[day(r["dispatched_at"])][r["singleton_kind"]] += 1
        singletons_per_day[day(r["dispatched_at"])]["total"] += 1

    large_failed = occupancy["large_failed"]["runs"]
    large_promoted = occupancy["large_promoted"]["runs"]
    trees = lineages(window, infos)
    complete_large = [t for t in trees if t["complete"] and t["root_size"] >= LARGE and t["runs"]]
    complete_failed = sum(t["failed_rounds"] for t in complete_large)
    complete_promoted = sum(len(t["promoted_runs"]) for t in complete_large)

    bisections, splits, quarantines = [], [], []
    partitions, suspects_distribution, by_strategy = [], Counter(), Counter()
    handoffs = []
    for cid in sorted(window):
        cand = window[cid]
        repairs = sorted(events(cand, "provisional_repair"), key=lambda e: e["observed_at"])
        for e in sorted(events(cand, "failure_partition"), key=lambda e: e["observed_at"]):
            details = e["details"]
            strategy, groups = details.get("strategy"), details.get("groups") or []
            following = [r for r in repairs if r["observed_at"] >= e["observed_at"]]
            if following:
                suspects = [s["pr"] for s in repair_suspects(following[0]["details"])]
            elif strategy in LOCATED_STRATEGIES and len(groups) >= 2:
                suspects = list(groups[0])
            else:
                suspects = []
            partitions.append(
                {
                    "candidate": cid,
                    "at": iso(e["observed_at"]),
                    "strategy": strategy,
                    "group_sizes": [len(g) for g in groups],
                    "suspects": len(suspects),
                    "suspect_prs": suspects,
                    "handed_off": bool(following),
                }
            )
            suspects_distribution[len(suspects)] += 1
            by_strategy[strategy] += 1
            entry = {
                "day": day(e["observed_at"]),
                "at": iso(e["observed_at"]),
                "candidate": cid,
                "size": infos[cid]["size"],
                "strategy": strategy,
                "group_sizes": [len(g) for g in groups],
                "reason": details.get("reason"),
                "unattributed_lines": diagnostics_lines(cand),
            }
            if strategy == "deterministic_bisection" and len(groups) >= 2:
                bisections.append(entry)
            elif strategy == "deterministic_bisection":
                entry["quarantined_prs"] = [q["details"]["pr"] for q in events(cand, "head_quarantined")]
                quarantines.append(entry)
            elif strategy in COMPONENT_STRATEGIES:
                splits.append(entry)
        for e in repairs:
            for suspect in repair_suspects(e["details"]):
                handoffs.append(readmission(merged, cid, e["observed_at"], suspect))
    per_day = defaultdict(Counter)
    for entry in bisections:
        per_day[entry["day"]]["bisections"] += 1
    for entry in splits:
        per_day[entry["day"]]["component_splits"] += 1
    for entry in quarantines:
        per_day[entry["day"]]["indivisible_quarantines"] += 1
    for d, counts in singletons_per_day.items():
        per_day[d]["singleton_runs"] = counts["total"]

    readmitted = [h for h in handoffs if h["readmitted_at"] is not None]
    withdrawn = [h for h in handoffs if h["readmitted_at"] is None]
    unclassified = sorted(
        ({"id": i["id"], "reason": i["unclassified_reason"]} for i in infos.values() if i["unclassified_reason"]),
        key=lambda u: u["id"],
    )
    non_runs = Counter(i["non_run_reason"] for i in infos.values() if i["non_run_reason"])
    dispatch_times = [r["dispatched_at"] for r in runs]
    end_times = [r["ended_at"] for r in runs if r["ended_at"]]
    return {
        "schema": SCHEMA,
        "generated_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "repo": merged.get("repo"),
        "base": merged.get("base"),
        "inputs": merged.get("inputs"),
        "data_horizon": iso(merged.get("horizon")),
        "window": {
            "candidates": len(window),
            "first_candidate": min(window) if window else None,
            "last_candidate": max(window) if window else None,
            "first_dispatch": iso(min(dispatch_times)) if dispatch_times else None,
            "last_end": iso(max(end_times)) if end_times else None,
            "runs": len(runs),
            "non_runs": sum(non_runs.values()),
        },
        "slot_occupancy": {
            "total_slot_minutes": total_minutes,
            "rows": occupancy,
            "green_run_minutes": distribution([r["minutes"] for r in runs if r["outcome"] == "promoted"]),
            "red_run_minutes": distribution([r["minutes"] for r in runs if r["outcome"] == "failed"]),
            "timing_sources": dict(Counter(r["timing_source"] for r in runs if r["timing_source"])),
        },
        "singletons": {
            "by_kind": singleton_table,
            "per_day": {d: dict(c) for d, c in sorted(singletons_per_day.items())},
        },
        "failed_rounds": {
            "large_failed_runs": large_failed,
            "large_promoted_runs": large_promoted,
            "window_aggregate": round(large_failed / large_promoted, 2) if large_promoted else None,
            "complete_large_lineages": len(complete_large),
            "complete_lineage_failed_runs": complete_failed,
            "complete_lineage_promoted_runs": complete_promoted,
            "complete_lineage_aggregate": round(complete_failed / complete_promoted, 2) if complete_promoted else None,
        },
        "lineages": trees,
        "per_day": {d: dict(c) for d, c in sorted(per_day.items())},
        "bisections": bisections,
        "component_splits": splits,
        "indivisible_quarantines": quarantines,
        "partitions": {
            "count": len(partitions),
            "suspects_distribution": {str(k): v for k, v in sorted(suspects_distribution.items())},
            "by_strategy": dict(by_strategy.most_common()),
            "handoffs": len(handoffs),
            "events": partitions,
        },
        "handoffs": {
            "count": len(handoffs),
            "readmitted": len(readmitted),
            "readmission_minutes": distribution([h["minutes"] for h in readmitted]),
            "still_withdrawn": len(withdrawn),
            "still_withdrawn_prs": sorted(h["pr"] for h in withdrawn),
            "events": handoffs,
        },
        "unclassified": {"count": len(unclassified), "candidates": unclassified},
        "non_runs": dict(non_runs.most_common()),
        "runs": [
            {k: (iso(v) if k in ("dispatched_at", "ended_at", "observed_at") else v) for k, v in r.items()}
            for r in sorted(runs, key=lambda r: r["id"])
        ],
        "candidates": [
            {k: (iso(v) if k in ("dispatched_at", "ended_at", "observed_at") else v) for k, v in i.items()}
            for i in sorted(infos.values(), key=lambda i: i["id"])
        ],
    }


def readmission(merged, handoff_candidate, handed_off_at, suspect):
    """Earliest later appearance of a suspect PR at a different head, or still withdrawn."""
    pr, head = suspect["pr"], suspect.get("head_sha")
    found = []
    for cand in merged["candidates"].values():
        for m in members(cand):
            if m.get("pr") != pr or (head and m.get("head_sha") == head):
                continue
            admitted = m.get("original_admitted_at")
            when = admitted if admitted and admitted > handed_off_at else cand.get("observed_at")
            if when and when > handed_off_at:
                found.append((when, f"member of candidate {cand['id']} at {m.get('head_sha', '')[:12]}"))
    for adm in merged["admissions"].values():
        snap = adm.get("snapshot") or {}
        if snap.get("pr") != pr or (head and snap.get("head_sha") == head) or snap.get("source") != "bors.admission":
            continue
        if adm["observed_at"] > handed_off_at:
            found.append((adm["observed_at"], f"admission {adm['id']} at {(snap.get('head_sha') or '')[:12]}"))
    when, evidence = min(found) if found else (None, None)
    return {
        "pr": pr,
        "handoff_candidate": handoff_candidate,
        "handed_off_at": iso(handed_off_at),
        "withdrawn_head": head,
        "readmitted_at": iso(when),
        "minutes": minutes(handed_off_at, when),
        "evidence": evidence,
    }


# --- rendering ------------------------------------------------------------------------------------


def table(headers, rows, align=None):
    align = align or ["---"] * len(headers)
    out = ["| " + " | ".join(headers) + " |", "| " + " | ".join(align) + " |"]
    for row in rows:
        out.append("| " + " | ".join("" if v is None else str(v) for v in row) + " |")
    return "\n".join(out)


def pct(share):
    return "" if share is None else f"{round(share * 100)}%"


def occupancy_table(report):
    rows = report["slot_occupancy"]["rows"]
    body = [
        [label, rows[key]["runs"], f"{rows[key]['slot_minutes']:.0f}", pct(rows[key]["share"])]
        for key, label in OCCUPANCY_ROWS
    ]
    body.append(
        [
            "Total",
            sum(rows[k]["runs"] for k, _ in OCCUPANCY_ROWS),
            f"{report['slot_occupancy']['total_slot_minutes']:.0f}",
            "100%",
        ]
    )
    return table(["Category", "Runs", "Slot minutes", "Share"], body, ["---", "---:", "---:", "---:"])


def render_markdown(report):
    out = [
        f"# Staging journal report: candidates {report['window']['first_candidate']} to {report['window']['last_candidate']}",
        "",
    ]
    out.append(
        f"Repository {report['repo']}, base `{report['base']}`. {report['window']['candidates']} candidates, "
        f"{report['window']['runs']} runs (dispatched candidates), {report['window']['non_runs']} non-runs. "
        f"First dispatch {report['window']['first_dispatch']}, last run end {report['window']['last_end']}. "
        f"Data horizon {report['data_horizon']}; generated {report['generated_at']}."
    )
    out.append("")
    inputs = ", ".join(f"{i['label']} (observed through {i['observed_through']})" for i in report["inputs"] or [])
    out.append(f"Inputs: {inputs}.")
    out.append("")
    out.append("## Slot occupancy")
    out.append("")
    out.append(f"The staging slot was occupied for {report['slot_occupancy']['total_slot_minutes']:.0f} minutes.")
    out.append("")
    out.append(occupancy_table(report))
    out.append("")
    green, red = report["slot_occupancy"]["green_run_minutes"], report["slot_occupancy"]["red_run_minutes"]
    out.append(
        f"Median run: {green.get('median')} minutes for a promoted run ({green.get('count')} runs), "
        f"{red.get('median')} minutes for a failed run ({red.get('count')} runs). "
        f"Timing sources: {report['slot_occupancy']['timing_sources']}."
    )
    out.append("")
    out.append("### Singletons by kind")
    out.append("")
    by_kind = report["singletons"]["by_kind"]
    out.append(
        table(
            ["Kind", "Runs", "Slot minutes", "Promoted", "Failed", "Cancelled or invalidated", "Candidates"],
            [
                [
                    kind,
                    by_kind[kind]["runs"],
                    f"{by_kind[kind]['slot_minutes']:.0f}",
                    by_kind[kind]["promoted"],
                    by_kind[kind]["failed"],
                    by_kind[kind]["cancelled_or_invalidated"],
                    ", ".join(str(c) for c in by_kind[kind]["candidates"]),
                ]
                for kind in SINGLETON_KINDS
            ],
            ["---", "---:", "---:", "---:", "---:", "---:", "---"],
        )
    )
    out.append("")
    fr = report["failed_rounds"]
    out.append("## Failed rounds per landed large batch")
    out.append("")
    out.append(
        "Lineages follow `parent_candidate_id`; the table lists lineages with a large root or more than one "
        "candidate (single-candidate singleton and small lineages are in the JSON only). "
        f"Window aggregate: {fr['large_failed_runs']} failed large runs / {fr['large_promoted_runs']} promoted large runs"
        f" = {fr['window_aggregate']}. Complete lineages with a large root: {fr['complete_large_lineages']}, "
        f"{fr['complete_lineage_failed_runs']} failed runs / {fr['complete_lineage_promoted_runs']} promoted runs"
        f" = {fr['complete_lineage_aggregate']}."
    )
    out.append("")
    out.append(
        table(
            [
                "Root",
                "Root size",
                "Candidates",
                "Runs",
                "Failed",
                "Cancelled/invalidated",
                "Promoted (sizes)",
                "Landed PRs",
                "Handed off",
                "Quarantined",
                "Complete",
                "Failed per promoted",
            ],
            [
                [
                    f"{t['root']}"
                    + (
                        f" (parent {t['root_parent_outside_window']} outside window)"
                        if t["root_parent_outside_window"]
                        else ""
                    ),
                    t["root_size"],
                    len(t["candidates"]),
                    t["runs"],
                    t["failed_rounds"],
                    t["cancelled_or_invalidated"],
                    ", ".join(str(p["size"]) for p in t["promoted_runs"]) or "none",
                    t["landed_prs"],
                    ", ".join(str(p) for p in t["handed_off_prs"]) or "",
                    ", ".join(str(p) for p in t["quarantined_prs"]) or "",
                    "yes" if t["complete"] else "no (open: " + ", ".join(str(c) for c in t["open_leaves"]) + ")",
                    t["failed_per_promoted"],
                ]
                for t in report["lineages"]
                if t["runs"] and (t["root_size"] >= LARGE or len(t["candidates"]) > 1)
            ],
            ["---", "---:", "---:", "---:", "---:", "---:", "---", "---:", "---", "---", "---", "---:"],
        )
    )
    out.append("")
    out.append("## Singletons, bisections and unlocated splits per day")
    out.append("")
    out.append(
        table(
            ["Day", "Singleton runs", "Deterministic bisections", "Component splits", "Indivisible quarantines"],
            [
                [
                    d,
                    c.get("singleton_runs", 0),
                    c.get("bisections", 0),
                    c.get("component_splits", 0),
                    c.get("indivisible_quarantines", 0),
                ]
                for d, c in report["per_day"].items()
            ],
            ["---", "---:", "---:", "---:", "---:"],
        )
    )
    out.append("")
    for title, entries in (
        ("Deterministic bisections", report["bisections"]),
        ("Component splits", report["component_splits"]),
    ):
        out.append(f"### {title}")
        out.append("")
        if not entries:
            out.append("None.")
        for entry in entries:
            out.append(
                f"- {entry['at']} candidate {entry['candidate']} ({entry['size']} members) split {entry['group_sizes']}"
                + (f", reason: {entry['reason']}" if entry.get("reason") else "")
                + "; unattributed lines: "
                + (
                    json.dumps(entry["unattributed_lines"])
                    if entry["unattributed_lines"]
                    else "none recorded (the parser produced no observation)"
                )
            )
        out.append("")
    out.append("## Suspects per failure partition")
    out.append("")
    parts = report["partitions"]
    out.append(
        f"{parts['count']} partitions, {parts['handoffs']} handoffs to repair. Partitions by strategy: {parts['by_strategy']}."
    )
    out.append("")
    out.append(
        table(["Suspects", "Partitions"], [[k, v] for k, v in parts["suspects_distribution"].items()], ["---:", "---:"])
    )
    out.append("")
    out.append("## Handoff to readmission")
    out.append("")
    h = report["handoffs"]
    rm = h["readmission_minutes"]
    out.append(
        f"{h['count']} suspects handed off; {h['readmitted']} readmitted (median {rm.get('median')} minutes, "
        f"min {rm.get('min')}, max {rm.get('max')}); {h['still_withdrawn']} still withdrawn as of {report['data_horizon']}"
        + (": PRs " + ", ".join(str(p) for p in h["still_withdrawn_prs"]) if h["still_withdrawn_prs"] else "")
        + "."
    )
    out.append("")
    out.append(
        table(
            ["PR", "Handoff candidate", "Handed off", "Readmitted", "Minutes", "Evidence"],
            [
                [
                    e["pr"],
                    e["handoff_candidate"],
                    e["handed_off_at"],
                    e["readmitted_at"] or "still withdrawn",
                    e["minutes"],
                    e["evidence"] or "",
                ]
                for e in h["events"]
            ],
            ["---:", "---:", "---", "---", "---:", "---"],
        )
    )
    out.append("")
    out.append("## Unclassified and non-run candidates")
    out.append("")
    u = report["unclassified"]
    if u["candidates"]:
        out.append(
            f"{u['count']} unclassified: " + "; ".join(f"{c['id']} ({c['reason']})" for c in u["candidates"]) + "."
        )
    else:
        out.append("No unclassified candidates.")
    out.append("")
    out.append("Non-runs by reason: " + (", ".join(f"{k}: {v}" for k, v in report["non_runs"].items()) or "none") + ".")
    out.append("")
    return "\n".join(out)


# --- fixture reducer ------------------------------------------------------------------------------


def reduce_candidate(cand):
    """Keep only the fields the report reads; see DETAIL_FIELDS, MEMBER_FIELDS and DROPPED_EVENT_KINDS."""
    snapshot = cand.get("snapshot") or {}
    kept_events = []
    for e in cand.get("events", []):
        if e["kind"] in DROPPED_EVENT_KINDS:
            continue
        fields = DETAIL_FIELDS.get(e["kind"], [])
        details = {k: e["details"][k] for k in fields if k in (e.get("details") or {})}
        if e["kind"] == "head_quarantined" and "quarantine" in e.get("details", {}):
            details["quarantine"] = {"reason": e["details"]["quarantine"].get("reason")}
        if e["kind"] == "failure_diagnostics" and "result" in details:
            result = details["result"] or {}
            details["result"] = {
                "complete": result.get("complete"),
                "jobs": [
                    {"job_id": j.get("job_id"), "observations": j.get("observations", [])}
                    for j in result.get("jobs") or []
                ],
            }
        kept_events.append(
            {
                "id": e.get("id"),
                "kind": e["kind"],
                "source": e["source"],
                "observed_at": e["observed_at"],
                "details": details,
            }
        )
    return {
        "id": cand["id"],
        "batch_id": cand.get("batch_id"),
        "generation": cand.get("generation"),
        "observed_at": cand.get("observed_at"),
        "runner_assigned_at": cand.get("runner_assigned_at"),
        "runner_started_at": cand.get("runner_started_at"),
        "runner_finished_at": cand.get("runner_finished_at"),
        "unknown_reason": cand.get("unknown_reason"),
        "snapshot": {
            "base": snapshot.get("base"),
            "base_sha": snapshot.get("base_sha"),
            "members": [{k: m.get(k) for k in MEMBER_FIELDS} for m in snapshot.get("members") or []],
        },
        "events": kept_events,
    }


def reduce_snapshot(merged, ids):
    """A feed-shaped document holding the chosen candidates and the admissions of their suspects."""
    missing = [i for i in ids if i not in merged["candidates"]]
    if missing:
        raise SystemExit(f"candidates not in the loaded journals: {missing}")
    candidates = [reduce_candidate(merged["candidates"][i]) for i in sorted(ids, reverse=True)]
    suspects = {
        s["pr"]
        for cand in candidates
        for e in cand["events"]
        if e["kind"] == "provisional_repair"
        for s in repair_suspects(e["details"])
    }
    admissions = [
        {
            "id": a["id"],
            "batch_id": a.get("batch_id"),
            "observed_at": a["observed_at"],
            "snapshot": {
                k: (a.get("snapshot") or {}).get(k)
                for k in ("pr", "head_sha", "source", "retry_reason", "original_admitted_at")
            },
        }
        for a in sorted(merged["admissions"].values(), key=lambda a: a["id"], reverse=True)
        if (a.get("snapshot") or {}).get("pr") in suspects
    ]
    return {
        "schema": FEED_SCHEMA,
        "repo": merged.get("repo"),
        "base": merged.get("base"),
        "timestamp_unit": "unix_microseconds",
        "reduced": {
            "by": "scripts/staging_journal_report.py --reduce",
            "inputs": merged.get("inputs"),
            "note": "Real candidates with members trimmed to MEMBER_FIELDS, events to DETAIL_FIELDS, and the admissions of handed-off suspects.",
        },
        "historical_unknown_reason": "no journal backfill; absent facts are unknown",
        "candidates": candidates,
        "admissions": admissions,
    }


def dump_fixture(doc):
    """JSON with one candidate or admission per line: diffable per candidate, a third the size of indented output."""
    lines = ["{"]
    scalar = {k: v for k, v in doc.items() if k not in ("candidates", "admissions")}
    for key, value in scalar.items():
        lines.append(f" {json.dumps(key)}: {json.dumps(value)},")
    for key in ("candidates", "admissions"):
        rows = [json.dumps(item, separators=(",", ":")) for item in doc[key]]
        body = ",\n".join("  " + row for row in rows)
        lines.append(f" {json.dumps(key)}: [\n{body}\n ]" + ("," if key == "candidates" else ""))
    lines.append("}")
    return "\n".join(lines) + "\n"


# --- command line ---------------------------------------------------------------------------------


def main(argv=None):
    parser = argparse.ArgumentParser(description="Staging triage measurements from the Bors candidate journal.")
    parser.add_argument("--journal", action="append", default=[], type=Path, help="saved feed snapshot (repeatable)")
    parser.add_argument("--url", action="append", default=[], help="live feed URL to fetch (repeatable)")
    parser.add_argument("--from-candidate", type=int)
    parser.add_argument("--to-candidate", type=int)
    parser.add_argument("--since", type=parse_utc, help="UTC timestamp; runs dispatched before it are excluded")
    parser.add_argument("--until", type=parse_utc, help="UTC timestamp; runs dispatched at or after it are excluded")
    parser.add_argument("--markdown", type=Path, help="write the Markdown report here (default: stdout)")
    parser.add_argument("--json", type=Path, help="write the JSON report here")
    parser.add_argument("--reduce", action="store_true", help="write a reduced fixture instead of a report")
    parser.add_argument("--candidates", help="comma-separated candidate ids for --reduce")
    parser.add_argument("--output", type=Path, help="fixture path for --reduce")
    args = parser.parse_args(argv)
    if not args.journal and not args.url:
        parser.error("at least one --journal or --url is required")
    snapshots = [load_journal(p) for p in args.journal] + [fetch_journal(u) for u in args.url]
    labels = [str(p) for p in args.journal] + list(args.url)
    merged = merge_snapshots(snapshots, labels)
    if args.reduce:
        if not args.candidates or not args.output:
            parser.error("--reduce needs --candidates and --output")
        ids = [int(x) for x in args.candidates.split(",") if x.strip()]
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(dump_fixture(reduce_snapshot(merged, ids)))
        print(f"wrote {args.output} ({args.output.stat().st_size} bytes, {len(ids)} candidates)")
        return 0
    window = select_window(merged["candidates"], args.from_candidate, args.to_candidate, args.since, args.until)
    if not window:
        raise SystemExit("no candidates in the selected window")
    report = analyse(merged, window)
    markdown = render_markdown(report)
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(report, indent=2) + "\n")
    if args.markdown:
        args.markdown.parent.mkdir(parents=True, exist_ok=True)
        args.markdown.write_text(markdown)
    else:
        sys.stdout.write(markdown)
    return 0


if __name__ == "__main__":
    sys.exit(main())

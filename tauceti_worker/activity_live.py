"""Minute-scale, disposable live spending. No journal-content reads in the sampler."""

from __future__ import annotations

import contextlib
import copy
import fcntl
import hashlib
import json
import os
import stat
import time
from decimal import Decimal
from pathlib import Path
from statistics import median

from .activity_summary import WINDOW_SECONDS, hourly_rate
from .budget import money
from .runtime_status import atomic_json, read_json
from .usage_accounting import read_summary, usage_path

INTERVAL = 60


def root():
    return Path(os.environ.get("TAUCETI_ACTIVITY_DIR", Path.home() / ".local/state/tauceti/activity"))


def cohort(model, phase, summary):
    return json.dumps([model, phase, summary.get("journal_format"), summary.get("prices_sha256")])


def calibrate(samples):
    """Only completed native, non-resumed samples enter this disposable cache."""
    groups = {}
    for sample in samples:
        for phase in (sample["phase"], None):
            key = cohort(sample["model"], phase, sample)
            groups.setdefault(key, []).append(money(sample["cost"]) / sample["bytes"])
    return {
        key: dict(usd_per_byte=str(median(rows[-128:])), samples=len(rows[-128:]))
        for key, rows in groups.items()
        if len(rows) >= 3
    }


@contextlib.contextmanager
def publication_lock():
    root().mkdir(parents=True, exist_ok=True)
    with (root() / "live-publication.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def baseline_path(path):
    return root() / "live-baselines" / (hashlib.sha256(str(path).encode()).hexdigest() + ".json")


def publication_context():
    from .budget import budget_dir

    return dict(view=read_json(budget_dir() / "live-sessions.json"), overlay=read_json(root() / "live-overlay.json"))


def publish_report(path, value, *, carry=None):
    """An immutable baseline plus its public view, sharing a brief publication lock."""
    output = value
    if carry and carry["overlay"].get("observed_at") is not None:
        view = carry["view"]
        base = value.get("all", {}).get("costs", {}).get("live_baseline") or {}
        prior = carry["overlay"].get("reports", {}).get(str(path), {})
        estimates = {}
        for key, session in view.get("sessions", {}).items():
            known = max(
                money(base.get("sessions", {}).get(key, {}).get("cost", "0")),
                money(session.get("cost", "0")),
                money(session.get("lower_bound_cost", "0")),
            )
            if session["status"] == "settled":
                estimates[key] = dict(
                    total=session["cost"],
                    observed=session["cost"],
                    source="settled",
                    observed_at=session.get("finished"),
                )
            else:
                estimates[key] = prior.get("sessions", {}).get(key) or dict(
                    total=str(known), observed=str(known), source="known-prefix", observed_at=None
                )
        output, _ = apply_overlay(
            value, view, base, prior, time.time(), estimates=estimates, **value.get("live_subject", {})
        )
        if output.get("all", {}).get("costs", {}).get("live_estimate"):
            output["all"]["costs"]["live_estimate"]["observed_at"] = carry["overlay"]["observed_at"]
    with publication_lock():
        atomic_json(baseline_path(path), value, durable=False)
        atomic_json(path, output, durable=False)


def publish_inputs(value, paths):
    costs = value["all"].get("costs", {})
    base = costs.get("live_baseline")
    if not base:
        return
    atomic_json(
        root() / "live-inputs.json",
        {**base, "rates": costs.get("live_rates", {}), "paths": [str(p) for p in paths]},
        durable=False,
    )


def publish_sessions(budget, cache, now):
    """Supervisor-owned active/terminal view, published at most once a minute."""
    if now - cache.get("live_published_at", float("-inf")) < INTERVAL:
        return
    inputs = read_json(root() / "live-inputs.json")
    previous = cache.get("live_sessions")
    if previous is None:
        previous = read_json(budget.directory / "live-sessions.json").get("sessions", {})
    pending = cache.get("live_pending", {})
    wanted = set(inputs.get("sessions", {})) | set(previous) | set(pending)
    with budget.locked():
        state = budget.current()
        rows = {s["id"]: dict(s) for s in budget.sessions(state, active=True)}
        for key in wanted - rows.keys():
            s = state["sessions"].get(key)
            if s and (
                s["status"] != "settled"
                or key in inputs.get("sessions", {})
                or inputs.get("ledger_sequence", 0)
                < pending.get(key, previous.get(key, {})).get("ledger_sequence", state["seq"])
            ):
                rows[key] = dict(s)
        sequence = state["seq"]
        info = budget.ledger.stat()
    ledger_identity = [info.st_dev, info.st_ino]
    from .budget_recovery import live_identity

    scope = os.environ.get("TAUCETI_FLEET_MACHINE_ID") or read_json(
        Path.home() / ".config/tauceti-fleet/host.json"
    ).get("machine_id", "local")
    values = {}
    for key, session in rows.items():
        path = usage_path(budget.directory, key, session.get("bridge"))
        external = bool(session.get("lifecycle", {}).get("external"))
        summary = (
            (read_summary(path, allow_foreign=external) if session["status"] != "settled" else None)
            or previous.get(key, {}).get("summary")
            or inputs.get("sessions", {}).get(key, {}).get("summary")
        )
        prior = previous.get(key, {})
        terminal_sequence = (
            prior["ledger_sequence"]
            if session["status"] == "settled"
            and prior.get("status") == "settled"
            and prior.get("cost") == session.get("cost")
            else pending.get(key, {}).get("ledger_sequence", sequence)
        )
        values[key] = dict(
            id=key,
            worker=session["worker"],
            scope=inputs.get("sessions", {}).get(key, {}).get("scope", scope),
            model=session["model"],
            phase=session["phase"],
            started=session["started"],
            status=session["status"],
            cost=session.get("cost", "0"),
            lower_bound_cost=session.get("lower_bound_cost", "0"),
            resume=session.get("resume"),
            finished=session.get("finished"),
            journal=str(path.with_suffix(".jsonl")),
            external=external,
            summary=summary,
            ledger_sequence=terminal_sequence,
            live=session["status"] != "settled" and live_identity(session, now, budget.directory)["live"],
        )
    atomic_json(
        budget.directory / "live-sessions.json",
        dict(version=1, observed_at=now, ledger_identity=ledger_identity, ledger_sequence=sequence, sessions=values),
        durable=False,
    )
    cache.update(live_published_at=now, live_sessions=values)
    cache["live_pending"] = {key: entry for key, entry in pending.items() if key in values}


def estimate(session, baseline, rates, now):
    """One lstat; prefer a published counter anchor, extrapolate only new bytes."""
    floor = max(
        money(baseline.get("cost", "0")), money(session.get("cost", "0")), money(session.get("lower_bound_cost", "0"))
    )
    if session["status"] == "settled":
        return dict(
            total=session["cost"],
            observed=session["cost"],
            source="settled",
            observed_at=session.get("finished") or now,
        )
    result = dict(total=str(floor), observed=str(floor), source="known-prefix", observed_at=baseline.get("observed_at"))
    summary = session.get("summary") or baseline.get("summary")
    if not summary or session.get("resume") or not summary.get("usage_seen"):
        return result
    try:
        info = Path(session["journal"]).lstat()
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            and not session.get("external")
            or [info.st_dev, info.st_ino] != summary["journal_identity"]
            or info.st_size < summary["journal_offset"]
            or summary["observed_at"] > now
        ):
            return {**result, "source": "journal-unavailable"}
    except (OSError, ValueError, TypeError, KeyError):
        return {**result, "source": "journal-unavailable"}
    known = max(floor, money(summary["cost"]))
    rate = rates.get(cohort(session["model"], session["phase"], summary)) or rates.get(
        cohort(session["model"], None, summary)
    )
    total = known
    if rate and session.get("live"):
        total = max(
            known, money(summary["cost"]) + money(rate["usd_per_byte"]) * (info.st_size - summary["journal_offset"])
        )
    return dict(
        total=str(total),
        observed=str(known),
        source="journal-size" if total > known else "usage-summary",
        observed_at=summary["observed_at"],
        file_size=info.st_size,
        file_mtime=info.st_mtime,
        file_identity=[info.st_dev, info.st_ino],
        usage_sequence=summary["sequence"],
    )


def portion(segment, since, until):
    begin, end, cost = segment
    if end == begin:
        return money(cost) if since <= end <= until else Decimal(0)
    return money(cost) * money(max(0, min(end, until) - max(begin, since))) / money(end - begin)


def apply_overlay(value, view, inputs, previous, now, *, worker=None, scope=None, estimates=None):
    """Pure rebase against a matching prepared generation; negative corrections replace estimates."""
    value = copy.deepcopy(value)
    costs = value.get("all", {}).get("costs", {})
    base = costs.get("live_baseline") or {}
    generation = base.get("generation")
    if (
        not generation
        or inputs.get("generation", generation) != generation
        or view.get("ledger_identity") != base.get("ledger_identity")
    ):
        return value, previous
    old = previous.get("sessions", {}) if previous.get("generation") == generation else {}
    samples, windows = (
        {},
        {key: dict(delta=Decimal(0), observed=Decimal(0), unobserved=Decimal(0)) for key in WINDOW_SECONDS},
    )
    counts = {}
    for key, session in view.get("sessions", {}).items():
        if worker is not None and session["worker"] != worker or scope is not None and session["scope"] != scope:
            continue
        baseline = base.get("sessions", {}).get(key, {})
        # A terminal row absent from the new baseline has already been included.
        if not baseline and base["ledger_sequence"] >= session["ledger_sequence"]:
            continue
        sample = estimates[key] if estimates is not None else estimate(session, baseline, inputs.get("rates", {}), now)
        # Recovery persists Decimal interval endpoints as strings; summaries use
        # JSON numbers. Keep the public timestamp type consistent across both.
        if sample.get("observed_at") is not None:
            sample = {**sample, "observed_at": float(sample["observed_at"])}
        if session["status"] != "settled":
            # An older optional summary may lag a newly prepared priced prefix.
            # Only a settled total may reduce already observed expenditure.
            sample = {
                **sample,
                "total": str(max(money(sample["total"]), money(baseline.get("cost", "0")))),
                "observed": str(max(money(sample["observed"]), money(baseline.get("cost", "0")))),
            }
        counts[sample["source"]] = counts.get(sample["source"], 0) + 1
        target = money(sample["total"]) - money(baseline.get("cost", "0"))
        prior = old.get(key, {})
        segments = list(prior.get("segments", []))
        accumulated = sum((money(s[2]) for s in segments), Decimal(0))
        if target < accumulated:
            # An authoritative observation can lower the previous heuristic.
            segments = (
                [[a, b, str(money(c) * target / accumulated)] for a, b, c in segments]
                if accumulated
                else [[min(baseline.get("observed_at") or session["started"], now), now, str(target)]]
            )
        elif target > accumulated:
            start = prior.get("sampled_at", baseline.get("observed_at") or session["started"])
            segments.append([min(start, now), now, str(target - accumulated)])
        samples[key] = {**sample, "sampled_at": now, "segments": segments}
        for name, seconds in WINDOW_SECONDS.items():
            since = now - seconds if seconds else 0
            delta = sum((portion(s, since, now) for s in segments), Decimal(0))
            if target < 0 and baseline.get("windows") and money(baseline.get("cost", "0")) > 0:
                delta = money(baseline["windows"].get(name, "0")) * target / money(baseline["cost"])
            windows[name]["delta"] += delta
            # Breakdown describes the contribution carried by this overlay;
            # previously observed live spend is already in the recorded rows.
            missing_observed = max(Decimal(0), money(sample["observed"]) - money(baseline.get("cost", "0")))
            fraction = missing_observed / target if target > 0 else Decimal(0)
            windows[name]["observed"] += max(Decimal(0), delta * min(Decimal(1), fraction))
            windows[name]["unobserved"] += max(Decimal(0), delta * (1 - min(Decimal(1), fraction)))
    spending = {}
    for key, recorded in costs.get("spending", {}).items():
        delta = windows[key]
        total = str(money(recorded["spent_usd"]) + delta["delta"]) if recorded.get("spent_usd") is not None else None
        spending[key] = {
            **recorded,
            "spent_usd": total,
            "recorded_spent_usd": recorded.get("spent_usd"),
            "live_observed_usd": str(delta["observed"]),
            "live_unobserved_estimate_usd": str(delta["unobserved"]),
            "live_adjustment_usd": str(delta["delta"]),
            "usd_per_hour": hourly_rate(total, recorded["covered_seconds"]),
            "usd_per_worker_hour": hourly_rate(total, recorded["worker_seconds"]),
        }
    costs["spending_including_live"] = spending
    costs["live_estimate"] = dict(
        observed_at=now,
        session_observed_at=view.get("observed_at"),
        sources=counts,
        baseline_generation=generation,
        refresh_seconds=INTERVAL,
        oldest_usage_at=min(
            (s["observed_at"] for s in samples.values() if s.get("observed_at") is not None), default=None
        ),
        stale=now - view.get("observed_at", 0) > 180,
    )
    return value, dict(generation=generation, sessions=samples)


def sample(*, budget_directory=None, now=None):
    """Manager-owned minute tick, independent of expensive historical collection."""
    from .budget import budget_dir

    now = time.time() if now is None else now
    saved = read_json(root() / "live-overlay.json")
    if now - saved.get("observed_at", float("-inf")) < INTERVAL:
        return
    inputs = read_json(root() / "live-inputs.json")
    view = read_json(Path(budget_directory or budget_dir()) / "live-sessions.json")
    if not view:
        return
    estimates = {
        key: estimate(session, inputs.get("sessions", {}).get(key, {}), inputs.get("rates", {}), now)
        for key, session in view.get("sessions", {}).items()
    }
    states = {}
    for name in inputs.get("paths", []):
        path = Path(name)
        baseline = read_json(baseline_path(path))
        subject = baseline.get("live_subject", {})
        output, state = apply_overlay(
            baseline, view, inputs, saved.get("reports", {}).get(name, {}), now, estimates=estimates, **subject
        )
        states[name] = state
        if "spending_including_live" not in output.get("all", {}).get("costs", {}):
            continue
        with publication_lock():
            fresh = read_json(baseline_path(path))
            if fresh.get("all", {}).get("costs", {}).get("live_baseline", {}).get("generation") != state.get(
                "generation"
            ):
                continue  # A newly prepared baseline wins; rebase next minute.
            atomic_json(path, output, durable=False)
    atomic_json(root() / "live-overlay.json", dict(observed_at=now, reports=states), durable=False)

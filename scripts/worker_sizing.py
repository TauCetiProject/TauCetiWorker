#!/usr/bin/env python3
"""One-day worker sizing experiment; run on the fleet host, outside worker scopes.

Only the ordinary worker admission ceiling is changed. Existing rounds finish.
Raw, compact observations are append-only; state and the one-page PDF are atomic.
No GitHub requests, production code deployment, or admission leases are involved.
"""

from __future__ import annotations

import argparse
import collections
import datetime as dt
import fcntl
import hashlib
import json
import math
import os
import shutil
import signal
import statistics
import subprocess
import time
from pathlib import Path

HOME = Path.home()
FLEET = HOME / ".local/state/tauceti-fleet"
ACTIVITY = HOME / ".local/state/tauceti/activity"
CONFIG = HOME / ".config/tauceti-fleet/host.json"
GIB = 1024**3
PLAN = [40, 80, 40, 20, 60, 40, 100, 40, 20, 80, 60, 40]


def read(path, default=None):
    try:
        return json.loads(Path(path).read_text())
    except FileNotFoundError:
        return default


def atomic(path, value):
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, separators=(",", ":")) + "\n")
    tmp.replace(path)


def append(path, value):
    with path.open("a") as stream:
        stream.write(json.dumps(value, separators=(",", ":")) + "\n")


def numbers(path):
    try:
        return {
            p[0].rstrip(":"): int(p[1]) for line in Path(path).read_text().splitlines() if len(p := line.split()) >= 2
        }
    except FileNotFoundError:
        return {}


def pressure(path):
    result = {}
    try:
        for line in Path(path).read_text().splitlines():
            name, *parts = line.split()
            result[name] = {k: float(v) for k, v in (p.split("=") for p in parts) if k in ("total", "avg60")}
    except FileNotFoundError:
        pass
    return result


def cgroup(path):
    if not path.exists():
        return None
    result = {
        "cpu": {
            k: v
            for k, v in numbers(path / "cpu.stat").items()
            if k in ("usage_usec", "user_usec", "system_usec", "nr_throttled", "throttled_usec")
        },
        "events": numbers(path / "memory.events"),
    }
    for name in ("memory.current", "memory.swap.current"):
        try:
            result[name] = int((path / name).read_text())
        except FileNotFoundError:
            result[name] = None
    result["memory"] = {
        k: v
        for k, v in numbers(path / "memory.stat").items()
        if k
        in ("anon", "file", "kernel", "pgfault", "pgmajfault", "workingset_refault_anon", "workingset_refault_file")
    }
    result["pressure"] = {name: pressure(path / f"{name}.pressure") for name in ("cpu", "memory", "io")}
    result["io"] = {}
    try:
        for line in (path / "io.stat").read_text().splitlines():
            dev, *parts = line.split()
            result["io"][dev] = {k: int(v) for k, v in (p.split("=") for p in parts)}
    except FileNotFoundError:
        pass
    return result


def sample():
    now = time.time()
    config, status = read(CONFIG), read(FLEET / "status.json")
    machine = status["machine_id"]
    prepared = read(ACTIVITY / "groups" / (hashlib.sha256(machine.encode()).hexdigest() + ".json"), {})
    activity = prepared.get("all", {})
    cost = activity.get("costs", {})
    scope = Path(status["worker_memory"]["cgroup"])
    workers = []
    for worker in status.get("workers", []):
        if not worker.get("alive"):
            continue
        group = cgroup(scope / worker["scope_unit"]) if worker.get("scope_unit") else None
        workers.append({k: worker.get(k) for k in ("id", "phase", "state", "scope_unit", "heartbeat_at")})
        workers[-1]["resources"] = group
    overview = read(FLEET / "ci-overview.json", {})
    lean = read(FLEET / "lean-statistics.json", {})
    # Compact prepared summaries; never copy journals, prompts, credentials or live baselines.
    return dict(
        at=now,
        status_at=status.get("observed_at", 0),
        status_error=status.get("error"),
        maximum=config["max_workers"],
        enabled=config["workers_enabled"],
        draining=config.get("draining"),
        admitted=status["active_worker_limit"],
        controller_release=status.get("controller_release"),
        workers=workers,
        memory=status["worker_memory"],
        workers_group=cgroup(scope),
        meminfo={
            k: v
            for k, v in numbers("/proc/meminfo").items()
            if k in ("MemTotal", "MemAvailable", "MemFree", "Cached", "Dirty", "Writeback", "SwapTotal", "SwapFree")
        },
        vmstat={
            k: v
            for k, v in numbers("/proc/vmstat").items()
            if k
            in (
                "pswpin",
                "pswpout",
                "pgmajfault",
                "pgscan_kswapd",
                "pgscan_direct",
                "pgsteal_kswapd",
                "pgsteal_direct",
                "oom_kill",
            )
        },
        cpu=[int(v) for v in Path("/proc/stat").read_text().splitlines()[0].split()[1:9]],
        cpus=os.cpu_count(),
        load=os.getloadavg(),
        pressure={name: pressure(f"/proc/pressure/{name}") for name in ("cpu", "memory", "io")},
        disks={
            p[2]: [int(v) for v in p[3:]]
            for line in Path("/proc/diskstats").read_text().splitlines()
            if len(p := line.split()) >= 14 and p[2] == "md127"
        },
        disk_free=os.statvfs(HOME).f_bavail * os.statvfs(HOME).f_frsize,
        activity=dict(
            prepared_at=activity.get("prepared_at", 0),
            errors=prepared.get("errors", []),
            rounds=activity.get("rounds", {}).get("total", {}),
            prs=activity.get("pr_windows", {}).get("total", {}),
            recorded=cost.get("spending", {}).get("total"),
            live=cost.get("spending_including_live", {}).get("total"),
            quality=cost.get("quality"),
            live_estimate=cost.get("live_estimate", {}),
        ),
        lean_at=lean.get("at"),
        lean=lean.get("workers", {}),
        ci=overview.get("sources", {}).get("backlog", {}),
        ci_running=status.get("ci_jobs_busy"),
        oom_events=[e["id"] for e in status.get("worker_isolation", {}).get("events", [])],
    )


def control(maximum):
    # Use exactly the host's installed implementation, without switching its release.
    release = (HOME / ".local/share/tauceti-fleet/current").resolve()
    code = (
        "import sys;sys.path.insert(0,sys.argv[1]);"
        "from tauceti_fleet.host import set_worker_maximum;"
        "print(set_worker_maximum(int(sys.argv[2])))"
    )
    subprocess.run(
        ["/run/current-system/sw/bin/python3", "-c", code, str(release), str(maximum)], check=True, timeout=30
    )


def fetch(url):
    # curl uses the host's working TLS/proxy setup; urllib is rejected by the public proxy.
    result = subprocess.run(
        ["curl", "--compressed", "-fsS", "--max-time", "25", "--max-filesize", "50000000", url],
        capture_output=True,
        check=True,
        timeout=30,
    )
    return json.loads(result.stdout)


def queue_sample(directory, start):
    result = {"at": time.time()}
    try:
        status = fetch("https://queue.taucetiproject.org/status")
        if status.get("schema") != "tauceti-queue.status/v1":
            raise ValueError("Unexpected queue status schema")
        result.update(
            counters=status.get("counters", {}),
            revision=status.get("service", {}).get("revision"),
            generated_at=status.get("generated_at"),
            conditions=status.get("conditions"),
            head={k: status.get("head", {}).get(k) for k in ("id", "size")} if status.get("head") else {},
            waiting=status.get("waiting", {}).get("count"),
            pending=status.get("admission", {}).get("pending"),
            queued=sum(b.get("size", 0) for b in status.get("open_batches", []))
            + (status.get("head") or {}).get("size", 0),
        )
    except Exception as exc:
        result["status_error"] = f"{type(exc).__name__}: {str(exc)[:300]}"
    try:
        feed = fetch("https://bors.taucetiproject.org/repositories/1/candidates?base=main")
        if feed.get("schema") != "tauceti-bors.candidates/v1" or feed.get("repo") != "TauCetiProject/TauCeti":
            raise ValueError("Unexpected candidate feed scope")
        admissions = feed.get("admissions", [])
        # Retain identities and times only. First insertion per PR/head is counted in reports.
        result["admissions"] = [
            dict(id=a["id"], at=a["observed_at"] / 1e6, pr=a["snapshot"]["pr"], head=a["snapshot"]["head_sha"])
            for a in admissions
        ]
        result["feed_oldest"] = min((a["at"] for a in result["admissions"]), default=None)
        inventory = read(ACTIVITY / "prs.json", {})
        machine = read(CONFIG)["machine_id"]
        result["host_authored_prs"] = sorted(
            {
                p["number"]
                for p in inventory.get("prs", [])
                if p.get("scope") == machine and p.get("repo") == "TauCetiProject/TauCeti"
            }
        )
    except Exception as exc:
        result["admissions_error"] = f"{type(exc).__name__}: {str(exc)[:300]}"
    append(directory / "queue.jsonl", result)
    return result


def avg(values):
    values = [v for v in values if v is not None and math.isfinite(v)]
    return statistics.mean(values) if values else None


def published_reviews(state_root, since):
    """Read local publication receipts; a repeat is not by itself unnecessary inference.

    The caller supplies the durable worker-state root, independently of runtime
    upgrades. A post/round pair is one archived published operation, not one API
    write. Re-publication receipts count separately from inference rounds.
    """
    records = []
    for store in state_root.glob("*/home/.cache/tauceti-review/*/store/TauCetiProject__TauCeti"):
        worker = store.relative_to(state_root).parts[0]
        for path in (store / "outbox/records/posts").glob("*/*.json"):
            post = read(path)
            at = dt.datetime.fromisoformat(post["posted_at"].replace("Z", "+00:00")).timestamp()
            if at < since or not post.get("scoreboard_comment_id"):
                continue
            round_path = store / "outbox/records/rounds" / str(post["pr"]) / f"{post['pr']}-{post['round']}.json"
            round_ = read(round_path, {})
            if round_.get("arm", "production") != "production" or post.get("repo") != "TauCetiProject/TauCeti":
                continue
            matching = round_.get("head_sha") == post.get("head_sha") and round_.get("scoreboard_sha256") == post.get(
                "scoreboard_body_sha256"
            )
            records.append(
                dict(
                    worker=worker,
                    at=at,
                    pr=post["pr"],
                    head=post["head_sha"],
                    round=post["round"],
                    scoreboard_hash=post.get("scoreboard_body_sha256"),
                    mode=round_.get("mode", "unknown") if matching else "unknown",
                    ran=round_.get("ran", []) if matching else [],
                    threads=post.get("threads", {}),
                    scoreboard=post["scoreboard_comment_id"],
                    by=round_.get("submitted_by"),
                )
            )
    # Retained copies of one worker receipt must not become extra operations.
    unique = {(r["worker"], r["pr"], r["round"], r["head"], r["at"]): r for r in records}
    return sorted(unique.values(), key=lambda r: r["at"])


def review_counts(records, start, end):
    selected = [r for r in records if start <= r["at"] < end]
    heads = {(r["pr"], r["head"]) for r in selected}
    inferred = {
        (r.get("worker"), r["pr"], r.get("round"), r["head"], r.get("scoreboard_hash")): r for r in selected if r["ran"]
    }
    return dict(
        published_operations=len(selected),
        distinct_pr_heads=len(heads),
        repeated_operations=len(selected) - len(heads),
        contest_operations=sum(r["mode"] == "reply" for r in selected),
        unclassified_operations=sum(r["mode"] == "unknown" for r in selected),
        inference_operations=len(inferred),
        rubric_inferences=sum(len(r["ran"]) for r in inferred.values()),
    )


def delta(a, b):
    return b - a if a is not None and b is not None and b >= a else None


def spent(sample, key="live"):
    return float((sample["activity"].get(key) or {}).get("spent_usd", "nan"))


def fresh(s):
    return s["at"] - s["status_at"] <= 60 and not s["status_error"]


def guard(samples, baseline):
    """Require five minutes of measured degradation, or two new OOM victims."""
    if len(samples) < 2:
        return None
    latest = samples[-1]
    recent = [s for s in samples if latest["at"] - s["at"] <= 330 and fresh(s)]
    if len(recent) < 2:
        return None
    if latest["memory"].get("oom_kill", 0) - recent[0]["memory"].get("oom_kill", 0) >= 2:
        return "two kernel OOM kills within five minutes"
    events = set(latest.get("oom_events", [])) - set(recent[0].get("oom_events", []))
    if len(events) >= 2:
        return "two userspace OOM victims within five minutes"
    if recent[-1]["at"] - recent[0]["at"] < 290 or any(
        b["at"] - a["at"] > 75 for a, b in zip(recent, recent[1:], strict=False)
    ):
        return None
    threshold = max(40, baseline + 10)
    if all(s["memory"].get("stall_full", 0) > threshold for s in recent):
        return f"worker full-memory stalls above {threshold:.0f}% for five minutes"
    return None


def ready(s, target, previous):
    # A lower ceiling does not cancel current rounds. Wait for those rounds to finish.
    active = sum(w["state"] in ("running", "surveying") for w in s["workers"])
    alive = len(s["workers"])
    if not fresh(s) or s["admitted"] != target:
        return False
    return active <= target and alive >= target


def rows(path):
    if not path.exists():
        return []
    with path.open() as stream:
        return [json.loads(line) for line in stream if line.strip()]


def metrics(a, b):
    seconds = b["at"] - a["at"]
    cpu_delta = [y - x for x, y in zip(a["cpu"], b["cpu"], strict=False)]
    total = sum(cpu_delta)
    values = dict(
        cpu=100 * (total - cpu_delta[3] - cpu_delta[4]) / total if total > 0 else None,
        iowait=100 * cpu_delta[4] / total if total > 0 else None,
        ram=(b["meminfo"]["MemTotal"] - b["meminfo"]["MemAvailable"]) * 1024 / GIB,
        swap=(b["meminfo"]["SwapTotal"] - b["meminfo"]["SwapFree"]) * 1024 / GIB,
        worker_ram=b["memory"]["charged"] / GIB,
        worker_swap=b["memory"]["swap"] / GIB,
        worker_memory_psi=b["memory"].get("stall_full"),
        swap_in=delta(a["vmstat"].get("pswpin"), b["vmstat"].get("pswpin")),
        swap_out=delta(a["vmstat"].get("pswpout"), b["vmstat"].get("pswpout")),
    )
    for name in ("cpu", "memory", "io"):
        d = delta(
            a["pressure"].get(name, {}).get("some", {}).get("total"),
            b["pressure"].get(name, {}).get("some", {}).get("total"),
        )
        values[name + "_psi"] = d / seconds / 10000 if d is not None else None
    # Swap counters count pages, independent of the vmstat CLI's display units.
    for key in ("swap_in", "swap_out"):
        values[key] = values[key] * os.sysconf("SC_PAGE_SIZE") / seconds / 1024**2 if values[key] is not None else None
    for dev, disk in b["disks"].items():
        old = a["disks"].get(dev)
        if old and len(disk) >= 11:
            values.update(
                disk_busy=(disk[9] - old[9]) / seconds / 10,
                disk_queue=(disk[10] - old[10]) / seconds / 1000,
                disk_read=(disk[2] - old[2]) * 512 / seconds / 1024**2,
                disk_write=(disk[6] - old[6]) * 512 / seconds / 1024**2,
            )
    return values


def phase_metrics(samples):
    totals = collections.defaultdict(lambda: collections.defaultdict(float))
    excluded = 0
    for a, b in zip(samples, samples[1:], strict=False):
        seconds = b["at"] - a["at"]
        if seconds <= 0 or seconds > 75 or not fresh(a) or not fresh(b):
            continue
        old = {w["id"]: w for w in a["workers"]}
        for worker in b["workers"]:
            previous = old.get(worker["id"])
            if not previous or not worker["resources"] or not previous["resources"]:
                continue
            phase = worker["phase"] or worker["state"] or "unknown"
            if (worker["phase"], worker["state"], worker["scope_unit"]) != (
                previous["phase"],
                previous["state"],
                previous["scope_unit"],
            ):
                excluded += 1
                continue
            r, p = worker["resources"], previous["resources"]
            used = delta(p["cpu"].get("usage_usec"), r["cpu"].get("usage_usec"))
            if used is None:
                continue
            t = totals[phase]
            t["seconds"] += seconds
            t["cpu_seconds"] += used / 1e6
            t["ram_seconds"] += (r["memory.current"] or 0) * seconds / GIB
            t["swap_seconds"] += (r["memory.swap.current"] or 0) * seconds / GIB
            for kind in ("cpu", "memory", "io"):
                d = delta(
                    p["pressure"].get(kind, {}).get("some", {}).get("total"),
                    r["pressure"].get(kind, {}).get("some", {}).get("total"),
                )
                if d is not None:
                    t[kind + "_psi_seconds"] += d / 1e6
            for dev, io in r["io"].items():
                if dev in p["io"]:
                    for kind in ("rbytes", "wbytes"):
                        d = delta(p["io"][dev].get(kind), io.get(kind))
                        if d is not None:
                            t[kind] += d
    return totals, excluded


def report(directory, samples=None):
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    state = read(directory / "state.json")
    if samples is None:
        samples = rows(directory / "samples.jsonl")
    queues = rows(directory / "queue.jsonl")
    review_snapshot = read(directory / "review-operations.json", {})
    review_records = review_snapshot.get("records", [])
    deployments = read(directory / "deployments.json", [])
    if not samples:
        return
    intervals = [
        (a, b, metrics(a, b))
        for a, b in zip(samples, samples[1:], strict=False)
        if 0 < b["at"] - a["at"] <= 75 and fresh(a) and fresh(b)
    ]
    phases, excluded = phase_metrics(samples)
    admissions = {a["id"]: a for q in queues for a in q.get("admissions", [])}
    authored = {p for q in queues for p in q.get("host_authored_prs", [])}
    seen_heads = {}
    for a in sorted(admissions.values(), key=lambda a: a["at"]):
        seen_heads.setdefault((a["pr"], a["head"]), a)
    plt.rcParams.update({"font.size": 8, "axes.spines.top": False, "axes.spines.right": False})
    fig = plt.figure(figsize=(11.7, 8.3), facecolor="white")
    fig.text(0.055, 0.96, "TAUCHONKI / WORKER SIZING", fontsize=18, weight="bold", color="#17324d")
    fig.text(
        0.055,
        0.936,
        f"Updated {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}   |   {state['status']}   |   "
        f"block {min(state['index'] + 1, len(state['plan']))}/{len(state['plan'])}, ceiling {state['expected']}   |   30s sampling / 5m report",
    )
    fig.text(
        0.055,
        0.913,
        "Two hours per achieved ceiling; reductions let active rounds finish. Plan: "
        + " → ".join(map(str, state["plan"]))
        + "; restore 40.",
        fontsize=8,
    )
    table_rows = []
    output = []
    for block in state["blocks"]:
        selected = [s for s in samples if s.get("block") == block["index"] and s.get("segment") == "measuring"]
        label = str(block["requested"])
        if block["target"] != block["requested"]:
            label += f"→{block['target']}"
        if len(selected) < 2:
            table_rows.append(
                [label, "warming" if not block.get("started") else "<1m", "—", "—", "—", "—", "—", "—", "—", "—"]
            )
            continue
        first, last = selected[0], selected[-1]
        hours = (last["at"] - first["at"]) / 3600
        start_pr, end_pr = first["activity"]["prs"], last["activity"]["prs"]
        counts = [delta(start_pr.get(k), end_pr.get(k)) for k in ("authored", "reviews", "fixes")]
        failed = sum(v.get("failed", 0) for v in last["activity"]["rounds"].values()) - sum(
            v.get("failed", 0) for v in first["activity"]["rounds"].values()
        )
        usd = spent(last) - spent(first)
        period = [
            m
            for a, b, m in intervals
            if b.get("block") == block["index"] and a.get("segment") == b.get("segment") == "measuring"
        ]
        psi = avg([m["worker_memory_psi"] for m in period])
        cpu = avg([m["cpu"] for m in period])
        q = [a for a in seen_heads.values() if first["at"] <= a["at"] <= last["at"]]
        qhost = sum(a["pr"] in authored for a in q)

        def fmt(v):
            return f"{v:.1f}" if v is not None and math.isfinite(v) else "—"

        table_rows.append(
            [
                label,
                f"{hours:.2f}",
                *(fmt(c / hours if c is not None else None) for c in counts),
                fmt(failed / hours),
                fmt(usd / hours),
                fmt(qhost / hours) if queues and any("admissions" in q for q in queues) else "—",
                fmt(cpu),
                fmt(psi),
            ]
        )
        output.append(
            dict(
                block=block["index"],
                requested=block["requested"],
                target=block["target"],
                hours=hours,
                authored=counts[0],
                reviews=counts[1],
                archived_reviews=review_counts(review_records, first["at"], last["at"]),
                fixes=counts[2],
                failed=failed,
                usd=usd,
                host_authored_queue_heads=qhost,
                cpu=cpu,
                worker_memory_full_psi=psi,
                phases=phase_metrics(selected)[0],
                authored_per_usd=counts[0] / usd if counts[0] is not None and usd > 0 else None,
                reviews_per_usd=counts[1] / usd if counts[1] is not None and usd > 0 else None,
                fixes_per_usd=counts[2] / usd if counts[2] is not None and usd > 0 else None,
            )
        )
    ax = fig.add_axes([0.05, 0.665, 0.9, 0.23])
    ax.axis("off")
    table = ax.table(
        cellText=table_rows,
        colLabels=[
            "Ceiling",
            "Hours",
            "PRs/h",
            "Reviews/h",
            "Fixes/h",
            "Failed/h",
            "API $/h*",
            "Queue/h†",
            "CPU %",
            "Mem stall %",
        ],
        loc="upper center",
        cellLoc="center",
    )
    table.auto_set_font_size(False)
    table.set_fontsize(7)
    table.scale(1, 0.85)
    for (r, _c), cell in table.get_celld().items():
        cell.set_edgecolor("#dfe6ed")
        if r == 0:
            cell.set_facecolor("#e6eef5")
            cell.set_text_props(weight="bold")
    if intervals:
        times = [(b["at"] - state["created"]) / 3600 for a, b, m in intervals]
        cpu_ax = fig.add_axes([0.07, 0.425, 0.40, 0.205])
        for key, name in [
            ("cpu", "CPU busy"),
            ("cpu_psi", "CPU pressure"),
            ("worker_memory_psi", "Worker memory full stalls"),
            ("io_psi", "I/O pressure"),
        ]:
            cpu_ax.plot(times, [m.get(key) for a, b, m in intervals], label=name, linewidth=1)
        cpu_ax.set(ylabel="% of time", xlabel="Hours since start", ylim=(0, 105))
        cpu_ax.legend(fontsize=6, ncol=2, loc="upper right")
        ram_ax = fig.add_axes([0.55, 0.425, 0.39, 0.205])
        for key, name in [
            ("ram", "Host used RAM"),
            ("worker_ram", "Worker charged RAM"),
            ("swap", "Host swap"),
            ("worker_swap", "Worker swap"),
        ]:
            ram_ax.plot(times, [m.get(key) for a, b, m in intervals], label=name, linewidth=1)
        ram_ax.set(ylabel="GiB", xlabel="Hours since start")
        ram_ax.legend(fontsize=6, ncol=2)
        for plot in (cpu_ax, ram_ax):
            for block in state["blocks"]:
                if block.get("started"):
                    x = (block["started"] - state["created"]) / 3600
                    plot.axvline(x, color="#b8c4cf", linewidth=0.6, linestyle="--")
            for deployment in deployments:
                plot.axvline((deployment["at"] - state["created"]) / 3600, color="#b05c23", linewidth=1, linestyle=":")
    phase_rows = []
    for phase, p in sorted(phases.items(), key=lambda item: -item[1]["seconds"])[:8]:
        seconds = p["seconds"]
        phase_rows.append(
            [
                phase,
                f"{seconds / 3600:.2f}",
                f"{p['cpu_seconds'] / seconds:.2f}",
                f"{p['ram_seconds'] / seconds:.2f}",
                f"{p['swap_seconds'] / seconds:.2f}",
                *(f"{100 * p[k + '_psi_seconds'] / seconds:.1f}" for k in ("cpu", "memory", "io")),
            ]
        )
    ax = fig.add_axes([0.055, 0.19, 0.89, 0.155])
    ax.axis("off")
    ax.text(0, 1.05, "RESOURCE USE BY WORKER PHASE (whole experiment; stable 30s intervals)", weight="bold", fontsize=9)
    if phase_rows:
        tab = ax.table(
            cellText=phase_rows,
            colLabels=[
                "Phase",
                "Worker h",
                "CPU cores/wkr",
                "RAM GiB/wkr",
                "Swap GiB/wkr",
                "CPU stall %",
                "Mem stall %",
                "I/O stall %",
            ],
            loc="upper center",
            cellLoc="center",
        )
        tab.auto_set_font_size(False)
        tab.set_fontsize(7)
        tab.scale(1, 0.8)
        for (r, _c), cell in tab.get_celld().items():
            cell.set_edgecolor("#dfe6ed")
            if r == 0:
                cell.set_facecolor("#e6eef5")
    latest = samples[-1]
    recent = [m for a, b, m in intervals[-10:]]

    def v(key):
        return f"{avg([m.get(key) for m in recent]):.1f}" if avg([m.get(key) for m in recent]) is not None else "—"

    qlatest = next((q for q in reversed(queues) if "counters" in q), {})
    qfirst = next((q for q in queues if "counters" in q), {})
    landed = delta(qfirst.get("counters", {}).get("prs_landed"), qlatest.get("counters", {}).get("prs_landed"))
    backlog = latest["ci"].get("value", {}).get("pr_builds", "?")
    fig.text(
        0.055,
        0.157,
        f"Latest 5m: root disk read/write {v('disk_read')}/{v('disk_write')} MiB/s; queue {v('disk_queue')}; "
        f"busy {v('disk_busy')}%; swap in/out {v('swap_in')}/{v('swap_out')} MiB/s. Free {latest['disk_free'] / GIB:.0f} GiB.",
        fontsize=7,
    )
    fig.text(
        0.055,
        0.137,
        f"CI PR backlog {backlog}; merge backlog {qlatest.get('queued', '?')}; global PR landings since first queue sample {landed if landed is not None else '?'}; "
        f"activity age {latest['at'] - latest['activity']['prepared_at']:.0f}s. {len(samples)} samples; {excluded} phase transitions excluded.",
        fontsize=7,
    )
    notes = [
        "* API includes live estimates and later accounting corrections; boundary totals are prepared (~1m lag). Failed rounds are separate from published output.",
        "† Queue/h: observed insertion per PR/head authored here (may be older work; up to 5m lag). Reviews/fixes can overlap; do not sum as unique PRs.",
        "Phase pressure is worker-time weighted 'some' PSI; table memory stall is worker-slice 'full' PSI. Charged RAM includes cache; root RAID busy is not saturation.",
        "Queue recovery and phase mix confound comparisons. Phase transitions are approximate. Per-scope I/O bytes unavailable; I/O pressure measured via PSI.",
    ]
    counts = review_counts(review_records, state["created"], samples[-1]["at"])
    if review_snapshot:
        fig.text(
            0.055,
            0.12,
            f"Archived reviews: {counts['published_operations']} published operations / {counts['distinct_pr_heads']} distinct PR-heads; "
            f"{counts['repeated_operations']} repeats (not all waste); {counts['contest_operations']} contest-mode operations. "
            f"Snapshot {time.strftime('%H:%M UTC', time.gmtime(review_snapshot['at']))}.",
            fontsize=7,
        )
    boundary = "; ".join(f"{time.strftime('%m-%d %H:%M UTC', time.gmtime(d['at']))} {d['label']}" for d in deployments)
    notes[-1] = (
        "Deployment boundary (orange): " + boundary
        if boundary
        else "Mirror repair not yet deployed; this is pre-repair data."
    )
    for i, note in enumerate(notes):
        fig.text(0.055, 0.108 - i * 0.017, note, fontsize=6.6, color="#465461")
    if state.get("hold_reason"):
        fig.text(0.055, 0.022, "Schedule: " + state["hold_reason"][:160], fontsize=7, color="#9b3c26")
    tmp = directory / "report.tmp.pdf"
    fig.savefig(tmp)
    plt.close(fig)
    tmp.replace(directory / "report.pdf")
    atomic(
        directory / "report.json",
        dict(
            updated=time.time(),
            blocks=output,
            phases=phases,
            excluded_phase_intervals=excluded,
            latest_queue=qlatest,
            archived_reviews=counts,
            review_snapshot_at=review_snapshot.get("at"),
            deployments=deployments,
        ),
    )


def run(directory, block_seconds):
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / "experiment.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        run_locked(directory, block_seconds)


def run_locked(directory, block_seconds):
    state = read(directory / "state.json")
    if state is None:
        initial = sample()
        if initial["maximum"] != 40 or not initial["enabled"] or initial["draining"] or not fresh(initial):
            raise RuntimeError("Start requires a fresh, enabled, undrained 40-worker fleet")
        state = dict(
            created=time.time(),
            status="running",
            plan=PLAN,
            index=0,
            expected=40,
            block_seconds=block_seconds,
            blocks=[],
            hold_reason=None,
            rejected_ceiling=None,
        )
        atomic(directory / "initial.json", initial)
    if state["status"] != "running":
        return
    if "pending_control" in state and read(CONFIG)["max_workers"] == state["pending_control"]:
        state["expected"] = state.pop("pending_control")
        atomic(directory / "state.json", state)
    stopping = False

    def stop(*_):
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    samples = rows(directory / "samples.jsonl")
    last_report = last_queue = 0
    while not stopping:
        tick = time.monotonic()
        try:
            s = sample()
            if s["maximum"] != state["expected"] or not s["enabled"] or s["draining"]:
                state.update(
                    status="stopped: external fleet control",
                    hold_reason="External fleet settings take precedence; no restoration applied",
                )
                atomic(directory / "state.json", state)
                report(directory, samples)
                return
            if not state["blocks"] or state["blocks"][-1]["index"] != state["index"]:
                target = state["plan"][state["index"]]
                reason = None
                recent = [x for x in samples[-12:] if fresh(x) and s["at"] - x["at"] <= 360]
                if target > 40:
                    if state.get("rejected_ceiling") and target >= state["rejected_ceiling"]:
                        target, reason = (
                            40,
                            f"Held: counts >= {state['rejected_ceiling']} previously caused deterioration",
                        )
                    elif len(recent) < 8 or not all(x["at"] - x["activity"]["prepared_at"] < 180 for x in recent):
                        target, reason = 40, "Higher count held: recent activity/resource coverage incomplete"
                    elif avg([x["memory"].get("stall_full") for x in recent]) > 35:
                        target, reason = 40, "Higher count held: baseline full-memory stalls above 35%"
                    elif guard(recent, 25):
                        target, reason = 40, "Higher count held: recent OOM/stall deterioration"
                previous = state["expected"]
                state["pending_control"] = target
                atomic(directory / "state.json", state)
                if target != previous:
                    control(target)
                state.update(expected=target, hold_reason=reason)
                state.pop("pending_control", None)
                state["blocks"].append(
                    dict(
                        index=state["index"],
                        requested=state["plan"][state["index"]],
                        target=target,
                        previous=previous,
                        requested_at=s["at"],
                        started=None,
                        reason=reason,
                    )
                )
                atomic(directory / "state.json", state)
                # Re-sample after control so the observation keeps its actual ceiling.
                s = sample()
            block = state["blocks"][-1]
            if not block["started"] and ready(s, block["target"], block["previous"]):
                block["started"] = s["at"]
                block["baseline_memory_psi"] = avg([x["memory"].get("stall_full") for x in samples[-20:]]) or s[
                    "memory"
                ].get("stall_full", 0)
            s.update(block=state["index"], segment="measuring" if block["started"] else "transition")
            append(directory / "samples.jsonl", s)
            samples.append(s)
            if block["target"] > 40:
                reason = guard(
                    [x for x in samples[-12:] if x["block"] == state["index"]], block.get("baseline_memory_psi", 25)
                )
                if reason:
                    control(40)
                    block.update(ended=s["at"], aborted=reason)
                    state.update(
                        expected=40,
                        rejected_ceiling=min(state.get("rejected_ceiling") or block["target"], block["target"]),
                        hold_reason=reason + "; returned ceiling to 40; active rounds finish",
                    )
                    state["index"] += 1
            if block.get("started") and s["at"] - block["started"] >= state["block_seconds"] and not block.get("ended"):
                block["ended"] = s["at"]
                state["index"] += 1
            if state["index"] >= len(state["plan"]):
                control(40)
                state.update(
                    expected=40, status="complete", finished=s["at"], hold_reason="Completed; restored ceiling to 40"
                )
            state["heartbeat"] = s["at"]
            state.pop("error", None)
            atomic(directory / "state.json", state)
            if time.time() - last_queue >= 300:
                queue_sample(directory, state["created"])
                last_queue = time.time()
            if time.time() - last_report >= 300 or state["status"] != "running":
                report(directory, samples)
                last_report = time.time()
            if state["status"] != "running":
                return
        except Exception as exc:
            state["error"] = f"{type(exc).__name__}: {str(exc)[:500]}"
            atomic(directory / "state.json", state)
            append(directory / "errors.jsonl", dict(at=time.time(), error=state["error"]))
            print(state["error"], flush=True)
        # No model/agent loop is needed to keep the experiment running all day.
        time.sleep(max(1, 30 - (time.monotonic() - tick)))
    # Stopping this experiment restores its initial ceiling only while we own it.
    if read(CONFIG)["max_workers"] == state["expected"]:
        control(40)
        state["expected"] = 40
    state.update(status="stopped", hold_reason="Experiment stopped; active work finishes normally")
    atomic(directory / "state.json", state)
    report(directory, samples)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["sample", "run", "report", "mirror", "reviews"])
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--block-seconds", type=int, default=7200)
    parser.add_argument("--host", default="tauchonki")
    parser.add_argument("--report-copy", type=Path, help="Also publish the mirrored PDF at this local path")
    parser.add_argument("--state-root", type=Path, help="Durable worker-state root for local review receipts")
    args = parser.parse_args()
    if args.command == "sample":
        print(json.dumps(sample(), separators=(",", ":")))
    elif args.command == "report":
        report(args.directory)
    elif args.command == "reviews":
        if not args.state_root:
            parser.error("reviews requires --state-root")
        state = read(args.directory / "state.json")
        atomic(
            args.directory / "review-operations.json",
            dict(
                at=time.time(),
                records=published_reviews(args.state_root, state["created"]),
            ),
        )
    elif args.command == "mirror":
        args.directory.mkdir(parents=True, exist_ok=True)
        while True:
            result = subprocess.run(
                [
                    "rsync",
                    "-az",
                    "--timeout=30",
                    "--exclude=venv",
                    "--exclude=mpl-cache",
                    "--exclude=experiment.lock",
                    "--exclude=worker_sizing.py",
                    "--exclude=*.tmp*",
                    "-e",
                    "ssh -o BatchMode=yes -o ConnectTimeout=10",
                    f"{args.host}:{args.directory}/",
                    f"{args.directory}/",
                ],
                timeout=90,
                check=False,
            )
            state = read(args.directory / "state.json", {})
            if result.returncode == 0:
                report(args.directory)
            if result.returncode == 0 and args.report_copy and (args.directory / "report.pdf").exists():
                temporary = args.report_copy.with_suffix(".tmp.pdf")
                shutil.copyfile(args.directory / "report.pdf", temporary)
                temporary.replace(args.report_copy)
            if result.returncode == 0 and state.get("status") and state["status"] != "running":
                return
            time.sleep(60)
    else:
        run(args.directory, args.block_seconds)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Read-only one-hour deployment observation; records evidence without changing workers."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from tauceti_worker.github_transport import atomic
from tauceti_worker.paths import ensure_ssl_cert_file


def health(environment):
    suffix = "-test" if environment == "test" else ""
    token = (Path.home() / (".config/tauceti-github-state" + suffix) / "client-token").read_text().strip()
    req = urllib.request.Request(
        "https://github-state" + suffix + ".taucetiproject.org/v1/health?repository=TauCetiProject/TauCeti",
        headers={"Authorization": "Bearer " + token, "User-Agent": "TauCetiWorker/1.0"},
    )
    with urllib.request.urlopen(req, timeout=20) as response:
        return json.load(response)


def observed_seconds(samples, interval=60):
    """Count sampled time, excluding interruptions rather than claiming their coverage."""
    return sum(
        delta
        for previous, current in zip(samples, samples[1:], strict=False)
        if 0
        < (delta := current["at"] - previous["at"])
        <= max(previous.get("interval", interval), current.get("interval", interval)) * 2
    )


def evaluate(samples, minimum=3600, interval=60):
    issues = []
    if observed_seconds(samples, interval) < minimum:
        issues.append("observation_window_incomplete")
    healthy = [
        r
        for r in samples
        if r.get("health", {}).get("state", {}).get("reconciliation_version") == 2
        and r.get("health", {}).get("quota", {}).get("reconciliation_version") == 2
    ]
    if len(healthy) != len(samples):
        issues.append("unavailable_or_old_deployment")
    if len(samples) < 12:
        issues.append("insufficient_health_samples")
    if healthy:
        last = healthy[-1]["health"]["state"]
        # Judge eligible requested gaps, not archival depth or real quota waits.
        waits = last.get("waits", [])
        quota_wait = any(
            r.get("lane") == "requested" and str(r.get("wait_reason", "")).startswith(("primary", "secondary"))
            for r in waits
        )
        pending = next((r for r in last.get("lanes", []) if r["lane"] == "requested"), {})
        if (
            pending.get("eligible")
            and pending.get("oldest_requested")
            and healthy[-1]["at"] * 1000 - pending["oldest_requested"] > 300000
            and not quota_wait
        ):
            issues.append("requested_gap_not_progressing")
        begin = healthy[0]["health"]["state"]
        if last.get("revision", 0) < begin.get("revision", 0):
            issues.append("revision_regressed")
        if last.get("last_repair", 0) <= begin.get("last_repair", 0):
            issues.append("no_repair_progress")
    return issues


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--environment", choices=["test", "production"], required=True)
    parser.add_argument("--hours", type=float, default=1)
    parser.add_argument("--version", help="Expected deployment ID in both Durable Objects")
    parser.add_argument("--interval", type=float, default=60)
    parser.add_argument("--resume", action="store_true", help="Resume saved samples, excluding unobserved gaps")
    parser.add_argument(
        "--state-dir", type=Path, default=Path.home() / ".local/state/tauceti/github-state-reconciliation"
    )
    args = parser.parse_args()
    if args.hours <= 0 or args.interval < 10:
        parser.error("hours must be positive and interval at least ten seconds")
    ensure_ssl_cert_file()
    os.umask(0o077)
    args.state_dir.mkdir(parents=True, exist_ok=True)
    samples = []
    began = None
    path = args.state_dir / (args.environment + "-observation.json")
    if args.resume and path.exists():
        saved = json.loads(path.read_text())
        if saved.get("environment") != args.environment or saved.get("version") != args.version:
            parser.error("saved observation environment or version differs")
        samples = saved.get("samples", [])
        began = saved.get("started_at") if samples else None
    while True:
        sample = {"at": time.time(), "interval": args.interval}
        try:
            sample["health"] = health(args.environment)
        except Exception as error:
            sample["error_type"] = type(error).__name__
            sample["http_status"] = getattr(error, "code", None)
        current = sample.get("health", {})
        ready = all(
            current.get(role, {}).get("reconciliation_version") == 2
            and (not args.version or current.get(role, {}).get("deployment_id") == args.version)
            for role in ("state", "quota")
        )
        if began is None and not ready:
            atomic(
                path,
                {
                    "environment": args.environment,
                    "status": "waiting_for_deployment",
                    "version": args.version,
                    "last_sample": sample,
                    "samples": [],
                    "complete": False,
                },
            )
            time.sleep(args.interval)
            continue
        if began is None:
            began = sample["at"]
        samples.append(sample)
        observed = observed_seconds(samples, args.interval)
        complete = observed >= args.hours * 3600
        issues = evaluate(samples, args.hours * 3600, args.interval)
        result = {
            "environment": args.environment,
            "version": args.version,
            "started_at": began,
            "samples": samples,
            "complete": complete,
            "observed_seconds": observed,
            "issues": issues,
            "status": "observing" if not complete else "passed" if not issues else "needs_investigation",
        }
        atomic(path, result)
        if complete:
            print(
                json.dumps(
                    {
                        "environment": args.environment,
                        "samples": len(samples),
                        "status": result["status"],
                        "issues": result["issues"],
                    }
                ),
                flush=True,
            )
            return 0 if result["status"] == "passed" else 1
        time.sleep(min(args.interval, max(0, args.hours * 3600 - observed)))


if __name__ == "__main__":
    raise SystemExit(main())

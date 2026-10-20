#!/usr/bin/env python3
"""Operator storage recovery for the shared GitHub state service.

Every command uses bounded, admin-authenticated service endpoints. None of
them sends a GitHub request or prints stored payloads or credentials.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from tauceti_worker.github_transport import atomic
from tauceti_worker.paths import ensure_ssl_cert_file

REPOSITORY = "TauCetiProject/TauCeti"
EVIDENCE = Path.home() / ".local/state/tauceti/github-state-recovery"


def service(environment):
    suffix = "-test" if environment == "test" else ""
    return "https://github-state" + suffix + ".taucetiproject.org", Path.home() / (
        ".config/tauceti-github-state" + suffix
    )


def call(environment, path, body=None, token="admin-token"):
    url, directory = service(environment)
    request = urllib.request.Request(
        url + path,
        data=None if body is None else json.dumps({"repository": REPOSITORY, **body}).encode(),
        method="GET" if body is None else "POST",
        headers={
            "Authorization": "Bearer " + (directory / token).read_text().strip(),
            "Content-Type": "application/json",
            "User-Agent": "TauCetiWorker/1.0 (+https://github.com/TauCetiProject/TauCetiWorker)",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        detail = error.read().decode(errors="replace")[:500]
        raise SystemExit(f"{path}: HTTP {error.code} {detail}") from None


def status(environment):
    return {
        "storage": call(environment, "/admin/storage?repository=" + REPOSITORY),
        "ingress": call(environment, "/admin/ingress?repository=" + REPOSITORY),
    }


def export(environment, tables, limit, recovery_id=None):
    # An existing recovery continues from its manifest; a new one records a bookmark.
    recovery = {"id": recovery_id} if recovery_id else call(environment, "/admin/storage/recovery", {})
    print(json.dumps({"recovery": recovery["id"], "bookmark": recovery.get("bookmark")}), file=sys.stderr, flush=True)
    for table in tables:
        while not (
            page := call(
                environment, "/admin/storage/export", {"recovery": recovery["id"], "table": table, "limit": limit}
            )
        )["complete"]:
            print(json.dumps(page), file=sys.stderr)
        while not (check := call(environment, "/admin/storage/verify", {"recovery": recovery["id"], "table": table}))[
            "verified"
        ]:
            print(json.dumps(check), file=sys.stderr)
        print(json.dumps(check), file=sys.stderr)
    return recovery


def cleanup(environment, cleanup_class, recovery, limit, batches):
    total = {"class": cleanup_class, "rows": 0, "logical_bytes": 0, "batches": 0}
    for _ in range(batches):
        body = {"class": cleanup_class, "limit": limit, **({"recovery": recovery} if recovery else {})}
        result = call(environment, "/admin/storage/cleanup", body)
        total["rows"] += result["rows"]
        total["logical_bytes"] += result.get("logical_bytes") or 0
        total["batches"] += 1
        total["database_size_after"] = result["database_size_after"]
        if result["rows"] < limit:
            break
    return total


def legacy_reads(environment):
    """Ordinary unmodified-client reads: stored state only, no direct fallback."""
    url, directory = service(environment)
    os.environ["TAUCETI_GITHUB_STATE_URL"] = url
    os.environ["TAUCETI_GITHUB_STATE_TOKEN_FILE"] = str(directory / "client-token")
    from tauceti_worker import github_state

    reads = {}
    for operation, args in [("open_prs", {}), ("open_issues", {})]:
        available, data = github_state.read(REPOSITORY, operation, **args)
        reads[operation] = {"available": available, "rows": len(data) if available and isinstance(data, list) else None}
    available, survey = github_state.read_survey(REPOSITORY)
    reads["survey"] = {"available": available, "rows": len(survey) if available else None}
    return reads


def receipt(environment, probe_bytes):
    health = call(environment, "/v1/health?repository=" + REPOSITORY, token="client-token")
    state = status(environment)
    evidence = {
        "at": time.time(),
        "environment": environment,
        "deployment_id": health["state"].get("deployment_id"),
        "storage_mode": health["state"]["storage"]["mode"],
        "schema": health["state"]["storage"]["schema"],
        "database_size": health["state"]["storage"]["database_size"],
        "legacy_client_reads": legacy_reads(environment),
        "probe": call(environment, "/admin/storage/probe", {"bytes": probe_bytes}) if probe_bytes else None,
        "ingress": state["ingress"],
        "legacy_cursors": state["storage"]["legacy_cursors"],
        "note": "Queue and DLQ depth are read from the Cloudflare dashboard or `wrangler queues info`; record them alongside this receipt.",
    }
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    path = EVIDENCE / f"receipt-{environment}-{int(evidence['at'])}.json"
    atomic(path, evidence)
    evidence["path"] = str(path)
    return evidence


def main():
    ensure_ssl_cert_file()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--environment", choices=["test", "production"], default="test")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("status")
    scan = commands.add_parser("scan")
    scan.add_argument("--table", required=True)
    scan.add_argument("--after", type=int, default=0)
    scan.add_argument("--limit", type=int, default=2000)
    exporting = commands.add_parser("export")
    exporting.add_argument("--table", action="append", required=True)
    exporting.add_argument("--limit", type=int, default=1000)
    exporting.add_argument("--recovery", help="continue an existing recovery manifest")
    cleaning = commands.add_parser("cleanup")
    cleaning.add_argument("--class", dest="cleanup_class", required=True)
    cleaning.add_argument("--recovery")
    cleaning.add_argument("--limit", type=int, default=100)
    cleaning.add_argument("--batches", type=int, default=10)
    probing = commands.add_parser("probe")
    probing.add_argument("--bytes", type=int, default=65536)
    ingress = commands.add_parser("ingress")
    ingress.add_argument("--mode", choices=["parked", "apply"], required=True)
    ingress.add_argument("--reason", default="operator")
    replay = commands.add_parser("replay")
    replay.add_argument("--held", action="store_true")
    replay.add_argument("--limit", type=int, default=25)
    reconcile = commands.add_parser("reconcile")
    reconcile.add_argument("--date", required=True, help="raw delivery date, YYYY-MM-DD")
    reconcile.add_argument("--cursor")
    reconcile.add_argument("--apply", action="store_true")
    receipting = commands.add_parser("receipt")
    receipting.add_argument("--probe-bytes", type=int, default=0)
    args = parser.parse_args()
    env = args.environment
    if args.command == "status":
        result = status(env)
    elif args.command == "scan":
        result = call(env, "/admin/storage/scan", {"table": args.table, "after": args.after, "limit": args.limit})
    elif args.command == "export":
        result = export(env, args.table, args.limit, args.recovery)
    elif args.command == "cleanup":
        result = cleanup(env, args.cleanup_class, args.recovery, args.limit, args.batches)
    elif args.command == "probe":
        result = call(env, "/admin/storage/probe", {"bytes": args.bytes})
    elif args.command == "ingress":
        result = call(env, "/admin/ingress", {"mode": args.mode, "reason": args.reason})
    elif args.command == "replay":
        result = call(env, "/admin/ingress/replay", {"held": args.held, "limit": args.limit})
    elif args.command == "reconcile":
        result = call(env, "/admin/ingress/reconcile", {"date": args.date, "cursor": args.cursor, "apply": args.apply})
    else:
        result = receipt(env, args.probe_bytes)
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()

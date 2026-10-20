#!/usr/bin/env python3
"""Resume a fenced S2 source export (no GitHub queries).

Defaults to read-only preflight. Production freeze/import/switch remain disabled
until S1 and its deployed acceptance evidence are integrated. Receipts contain
identities, checkpoints and checksums; never SQL rows or connection strings.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

# Reuse the existing credential locations, without its HTTP error-body logging.
from storage_recovery import service

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from tauceti_worker.paths import ensure_ssl_cert_file


def call(environment, body):
    url, directory = service(environment)
    request = urllib.request.Request(
        url + "/admin/migration",
        data=json.dumps(body).encode(),
        headers={
            "Authorization": "Bearer " + (directory / "admin-token").read_text().strip(),
            "Content-Type": "application/json",
            "User-Agent": "TauCetiWorker/1.0 (+https://github.com/TauCetiProject/TauCetiWorker)",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        # Do not print arbitrary proxy/driver error bodies or request details.
        raise SystemExit(f"migration request failed: HTTP {error.code}; inspect authenticated status") from None
    except (urllib.error.URLError, OSError):
        raise SystemExit("migration transport failed; retry the same generation") from None


def main():
    ensure_ssl_cert_file()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--environment", choices=["test", "production"], required=True)
    parser.add_argument("--repository", default="TauCetiProject/TauCeti")
    parser.add_argument("--generation", help="explicit UUID v4; reuse it after interruption")
    parser.add_argument("--target", help="opaque project/branch/database label; never a DSN")
    parser.add_argument("--apply", action="store_true", help="perform the selected operation (test export only)")
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--steps", type=int, default=1, help="bounded export/verification steps per invocation")
    parser.add_argument(
        "command", choices=["preflight", "status", "freeze", "resume", "abandon"], nargs="?", default="preflight"
    )
    args = parser.parse_args()
    if not 1 <= args.limit <= 250 or not 1 <= args.steps <= 10000:
        parser.error("limit must be 1..250 and steps 1..10000")
    mutating = args.command in {"freeze", "resume", "abandon"} and args.apply
    if mutating and (not args.generation or not args.target):
        parser.error("--apply requires --generation and --target")
    body = {"repository": args.repository, "environment": args.environment}
    if not mutating:
        body["operation"] = "status" if args.command == "status" else "preflight"
    else:
        body.update(
            operation={"freeze": "freeze", "resume": "step", "abandon": "abandon"}[args.command],
            generation=args.generation,
            target=args.target,
            apply=True,
            limit=args.limit,
        )
    for _ in range(args.steps if mutating and args.command == "resume" else 1):
        result = call(args.environment, body)
        print(json.dumps(result, sort_keys=True), flush=True)
        if result.get("phase") in {"source_verified", "abandoned"}:
            break


if __name__ == "__main__":
    main()

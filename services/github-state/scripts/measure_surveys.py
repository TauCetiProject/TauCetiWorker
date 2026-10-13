#!/usr/bin/env python3
"""Measure compact mirror surveys and compare sampled facts with full stored histories.

Uses the client endpoint, not GitHub or the reference API. Output contains sizes,
latencies, coverage and differences, never credentials or comment contents.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.parse
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from tauceti_worker.github_transport import atomic
from tauceti_worker.paths import ensure_ssl_cert_file
from tauceti_worker.survey_facts import summarize


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--environment", choices=["test", "production"], default="test")
    parser.add_argument("--roadmap", action="append", required=True)
    parser.add_argument("--compare", type=int, default=12)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not 0 <= args.compare <= 100:
        parser.error("compare must be between zero and 100")
    ensure_ssl_cert_file()
    suffix = "-test" if args.environment == "test" else ""
    base = "https://github-state" + suffix + ".taucetiproject.org"
    token = (Path.home() / (".config/tauceti-github-state" + suffix) / "client-token").read_text().strip()
    headers = {
        "Authorization": "Bearer " + token,
        "User-Agent": "TauCetiWorker/1.0 (+https://github.com/TauCetiProject/TauCetiWorker)",
    }
    repo = "TauCetiProject/TauCeti"
    measurements = []

    def query(operation, **query_args):
        began = time.monotonic()
        cursor, seen, rows, requests, size, revision = None, set(), [], 0, 0, None
        while True:
            params = {"repository": repo, "operation": operation, "args": json.dumps(query_args)}
            if cursor:
                params["cursor"] = cursor
            request = urllib.request.Request(base + "/v1/query?" + urllib.parse.urlencode(params), headers=headers)
            with urllib.request.urlopen(request, timeout=40) as response:
                raw = response.read()
            value = json.loads(raw)
            requests += 1
            size += len(raw)
            if value.get("status") != "ok" or not value.get("complete"):
                raise RuntimeError(operation + " coverage unavailable")
            if revision is not None and revision != value["revision"]:
                raise RuntimeError("mixed pagination revision")
            revision = value["revision"]
            rows.extend(value["data"])
            cursor = value.get("cursor")
            if not cursor:
                break
            if cursor in seen:
                raise RuntimeError("repeated cursor")
            seen.add(cursor)
        measurements.append(
            {
                "operation": operation,
                "args": query_args,
                "rows": len(rows),
                "requests": requests,
                "bytes": size,
                "seconds": round(time.monotonic() - began, 4),
                "revision": revision,
            }
        )
        return rows

    scopes, samples, preferred = [], {}, []
    for roadmap in args.roadmap:
        rows = query("survey", roadmap=roadmap, skip="", numbers="")
        # The second read measures the warm projection rather than local migration.
        rows = query("survey", roadmap=roadmap, skip="", numbers="")
        scopes.append(
            {
                "roadmap": roadmap,
                **measurements[-1],
                "issue_gaps": sum(not r["review"]["issue_complete"] for r in rows),
                "review_gaps": sum(not r["review"]["review_complete"] for r in rows),
            }
        )
        for row in rows:
            if row["review"]["issue_complete"] and row["review"]["review_complete"]:
                samples[row["pr"]["number"]] = row
        assigned = [
            r["pr"]["number"]
            for r in rows
            if r["pr"]["number"] in samples
            and any(label.get("name") == "roadmap/" + roadmap for label in r["pr"].get("labels", []))
        ]
        if assigned:
            preferred.extend([assigned[0], assigned[-1]])
    comparisons = []
    numbers = list(dict.fromkeys([*preferred, *sorted(samples)]))[: args.compare]
    for number in numbers:
        row = samples[number]
        histories = [query(operation, number=number) for operation in ("issue_comments", "review_comments")]
        expected = summarize(*histories)
        if expected["contest"]:
            expected["contest"]["id"] = str(expected["contest"]["id"])
        actual = {k: row["review"][k] for k in expected}
        changed = []
        if actual != expected:
            latest = query("survey", roadmap="any", skip="", numbers=str(number))
            if latest:
                actual = {k: latest[0]["review"][k] for k in expected}
                changed = [k for k in expected if row["review"][k] != actual[k]]
        differences = [k for k in expected if actual[k] != expected[k]]
        comparisons.append(
            {
                "number": number,
                "issue_comments": len(histories[0]),
                "review_comments": len(histories[1]),
                "differences": differences,
                "changed_during_sample": changed,
            }
        )
    result = {
        "sample": datetime.now(UTC).isoformat(),
        "environment": args.environment,
        "scopes": scopes,
        "comparisons": comparisons,
        "measurements": measurements,
        "mismatches": sum(bool(c["differences"]) for c in comparisons),
    }
    atomic(args.output, result)
    print(json.dumps({k: v for k, v in result.items() if k != "measurements"}, indent=2))
    if result["mismatches"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()

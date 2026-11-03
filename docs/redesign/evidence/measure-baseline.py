#!/usr/bin/env python3
"""Measure a bounded canonical landing sample without GitHub's merge-state bias.

Usage: python measure-baseline.py --git-dir /path/to/bare.git --output baseline.json
Optional --merged-json and --open-json accept gh pr list JSON for comparison.
The caller owns fetching the repository; this script performs no mutation/fetch.
Git may lazily fetch blobs if the supplied repository is a partial clone.
"""

import argparse
from collections import Counter
from datetime import datetime, timedelta, timezone
import json
import math
from pathlib import Path
import re
import statistics
import subprocess


def distribution(values):
    ordered = sorted(values)
    if not ordered:
        return {"count": 0}
    return {
        "count": len(ordered),
        "sum": sum(ordered),
        "mean": round(statistics.mean(ordered), 3),
        "median": statistics.median(ordered),
        "p90": ordered[math.ceil(len(ordered) * 0.90) - 1],
        "p95": ordered[math.ceil(len(ordered) * 0.95) - 1],
        "max": ordered[-1],
        "at_least_1500": sum(value >= 1500 for value in ordered),
        "at_least_2000": sum(value >= 2000 for value in ordered),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--git-dir", required=True)
    parser.add_argument("--ref", default="HEAD")
    parser.add_argument("--limit", type=int, default=500)
    parser.add_argument("--merged-json", type=Path)
    parser.add_argument("--open-json", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.limit < 2:
        parser.error("--limit must be at least 2")
    git = ["git", f"--git-dir={args.git_dir}"]
    tip = subprocess.check_output(git + ["rev-parse", args.ref], text=True).strip()
    log = subprocess.check_output(
        git
        + [
            "log", "--first-parent", f"-{args.limit}",
            "--format=RECORD%x09%H%x09%cI%x09%s", "--numstat", "--no-renames", tip,
        ],
        text=True,
    )
    rows = []
    for line in log.splitlines():
        if line.startswith("RECORD\t"):
            _, sha, committed_at, subject = line.split("\t", 3)
            match = re.search(r"\(#(\d+)\)$", subject)
            rows.append({
                "sha": sha, "committed_at": committed_at, "subject": subject,
                "inferred_pr": int(match.group(1)) if match else None,
                "added": 0, "deleted": 0, "lean_added": 0, "lean_deleted": 0,
                "binary_files": 0, "quoted_paths": 0,
            })
            continue
        fields = line.split("\t", 2)
        if not rows or len(fields) != 3:
            continue
        added, deleted, path = fields
        if not (added.isdigit() and deleted.isdigit()):
            rows[-1]["binary_files"] += 1
            continue
        if path.startswith('"'):
            # Git's C-quoted names need a dedicated parser before path classification.
            rows[-1]["quoted_paths"] += 1
            continue
        rows[-1]["added"] += int(added)
        rows[-1]["deleted"] += int(deleted)
        if path.endswith(".lean"):
            rows[-1]["lean_added"] += int(added)
            rows[-1]["lean_deleted"] += int(deleted)
    if len(rows) < 2:
        raise SystemExit("Not enough canonical commits for a bounded sample")
    if any(row["quoted_paths"] for row in rows):
        raise SystemExit("Quoted Git paths need classification; refusing incomplete LOC counts")
    selected = [r for r in rows if r["inferred_pr"] and r["lean_added"] + r["lean_deleted"] > 0]
    occurrences = Counter(r["inferred_pr"] for r in selected)
    times = [datetime.fromisoformat(r["committed_at"]) for r in rows]
    oldest, newest = min(times), max(times)
    first_hour = oldest.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    end_hour = newest.replace(minute=0, second=0, microsecond=0)
    full_hours = []
    cursor = first_hour
    while cursor < end_hour:
        following = cursor + timedelta(hours=1)
        full_hours.append({
            "start": cursor.isoformat(),
            "count": sum(cursor <= datetime.fromisoformat(r["committed_at"]) < following for r in selected),
        })
        cursor = following
    report = {
        "schema": "tauceti.redesign-baseline/v1",
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "repository": "TauCetiProject/TauCeti",
        "canonical_ref": args.ref,
        "canonical_tip": tip,
        "selection": "Consecutive first-parent canonical commits; PR identity inferred from final (#N) subject suffix; Lean PR subset changes .lean files",
        "rename_policy": "--no-renames; moves count as deletion plus addition",
        "requested_commit_count": args.limit,
        "commit_count": len(rows),
        "oldest_commit_at": oldest.isoformat(),
        "newest_commit_at": newest.isoformat(),
        "lean_pr_commit_count": len(selected),
        "unique_inferred_lean_pr_count": len(occurrences),
        "repeated_inferred_prs": {str(k): v for k, v in occurrences.items() if v > 1},
        "unassociated_or_nonlean_commits": len(rows) - len(selected),
        "binary_file_count": sum(r["binary_files"] for r in rows),
        "lean_additions": distribution([r["lean_added"] for r in selected]),
        "lean_deletions": distribution([r["lean_deleted"] for r in selected]),
        "lean_churn": distribution([r["lean_added"] + r["lean_deleted"] for r in selected]),
        "all_file_additions": distribution([r["added"] for r in selected]),
        "complete_clock_hours": full_hours,
        "complete_clock_hour_counts": distribution([r["count"] for r in full_hours]),
        "commits": rows,
    }
    for label, path in (("github_merged_comparison", args.merged_json), ("github_open_comparison", args.open_json)):
        if path:
            prs = json.loads(path.read_text())
            report[label] = {
                "selection": "gh pr list default ordering, no search filter; all file types; not a random sample",
                "count": len(prs),
                "additions": distribution([r["additions"] for r in prs]),
                "churn": distribution([r["additions"] + r["deletions"] for r in prs]),
                "oldest_merged_at": min((r["mergedAt"] for r in prs if r.get("mergedAt")), default=None),
                "newest_merged_at": max((r["mergedAt"] for r in prs if r.get("mergedAt")), default=None),
                "prs": prs,
            }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({k: v for k, v in report.items() if k not in {"commits", "github_merged_comparison", "github_open_comparison"}}, indent=2))


if __name__ == "__main__":
    main()

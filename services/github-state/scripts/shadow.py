#!/usr/bin/env python3
"""Compare mirrors with budget-owned uncached test reads; never gate model spending.

Only fingerprints and discrepancy categories enter sample logs. Private pagination
checkpoints live separately, and reference requests use no worker/App credentials.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime
import hashlib
import json
import os
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from tauceti_worker import github_transport as transport
from tauceti_worker.github_state import read
from tauceti_worker.paths import ensure_ssl_cert_file


@contextlib.contextmanager
def gateway(url, token_file):
    keys = ("TAUCETI_GITHUB_STATE_URL", "TAUCETI_GITHUB_STATE_TOKEN_FILE")
    previous = {k: os.environ.get(k) for k in keys}
    os.environ.update(dict(zip(keys, (url, str(token_file)), strict=True)))
    try:
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def canonical(operation, row):
    """Comparable cross-API fields, not response bodies or transport-only metadata."""
    if operation in ("open_prs", "pr"):
        return {k: row.get(k) for k in ("number", "state", "isDraft", "headRefOid", "title", "body")}
    if operation == "review_threads":
        comments = row.get("comments", {}).get("nodes", [])
        return {
            "id": row.get("id"),
            "isResolved": row.get("isResolved"),
            "isOutdated": row.get("isOutdated"),
            "comments": sorted(
                (str(n.get("fullDatabaseId")), n.get("body"), str((n.get("replyTo") or {}).get("fullDatabaseId")))
                for n in comments
            ),
        }
    if operation in ("issue_comments", "review_comments"):
        return {
            k: str(row.get(k)) if k in ("id", "in_reply_to_id") else row.get(k)
            for k in ("id", "body", "in_reply_to_id", "path", "line", "commit_id")
        }
    if operation == "reactions":
        return {"id": str(row.get("id")), "content": row.get("content")}
    if operation == "statuses":
        result = {k: row.get(k) for k in ("context", "state", "startedAt")}
        if isinstance(result["startedAt"], str):
            try:
                stamp = datetime.datetime.fromisoformat(result["startedAt"].replace("Z", "+00:00"))
                if stamp.tzinfo is not None:
                    result["startedAt"] = stamp.astimezone(datetime.UTC).isoformat(timespec="microseconds")
            except ValueError:
                pass
        return result
    if operation in ("checks", "workflow_runs", "workflow_jobs"):
        fields = ("id", "name", "status", "conclusion", "head_sha")
        if operation == "workflow_runs":
            fields += ("run_attempt", "head_branch")
        elif operation == "workflow_jobs":
            fields += ("run_id",)
        return {k: str(row.get(k)) if k in ("id", "run_id") else row.get(k) for k in fields}
    return row


def fingerprints(operation, data):
    rows = data if isinstance(data, list) else [data]
    result = {}
    for row in rows:
        value = canonical(operation, row)
        key = str(
            row.get("number")
            if operation in ("pr", "open_prs")
            else row.get("context")
            if operation == "statuses"
            else row.get("id")
        )
        result[key] = hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return result


class Reference:
    def __init__(self, directory, state_dir, limit=16):
        self.url = "https://github-state-test.taucetiproject.org/admin/reference"
        self.token = (directory / "admin-token").read_text().strip()
        self.state_dir = state_dir
        self.left = limit
        self.points = 0
        self.requests = 0
        self.local_deferrals = 0
        self.deferred = None
        self.workflow_candidates = []

    def read(self, repo, operation, **args):
        since = args.pop("_since", None)
        query = {"repository": repo, "operation": operation, "args": args}
        identity = hashlib.sha256(json.dumps([query, since], sort_keys=True).encode()).hexdigest()
        path = self.state_dir / "reference-checkpoints" / (identity + ".json")
        try:
            checkpoint = json.loads(path.read_text())
        except (OSError, ValueError):
            checkpoint = {}
        if time.time() - checkpoint.get("started", time.time()) > 3600:
            checkpoint = {}
        checkpoint.setdefault("started", time.time())
        checkpoint.setdefault("fingerprints", {})
        checkpoint.setdefault("page", 1)
        checkpoint.setdefault("seen", [])
        while self.left:
            request = urllib.request.Request(
                self.url,
                data=json.dumps(
                    {
                        "query": query,
                        **({"since": since} if since else {}),
                        **{k: checkpoint[k] for k in ("page", "cursor", "api", "nested") if k in checkpoint},
                    }
                ).encode(),
                headers={
                    "Authorization": "Bearer " + self.token,
                    "Content-Type": "application/json",
                    "User-Agent": "TauCetiWorker/1.0",
                },
            )
            deadline = time.monotonic() + 5
            while True:
                with urllib.request.urlopen(request, timeout=20) as response:
                    result = json.load(response)
                if result.get("category") != "capacity":
                    break
                self.local_deferrals += 1
                if time.monotonic() >= deadline:
                    self.deferred = "capacity"
                    transport.atomic(path, checkpoint)
                    return None
                time.sleep(max(0.05, min(0.5, result.get("retry_at", time.time() * 1000 + 250) / 1000 - time.time())))
            self.left -= 1
            self.requests += 1
            self.points += result.get("points") or 0
            if result.get("deferred") or result.get("unavailable"):
                self.deferred = result.get("category", "upstream")
                transport.atomic(path, checkpoint)
                return None
            if result.get("restart"):
                checkpoint = {"started": time.time(), "page": 1, "api": result["api"], "seen": [], "fingerprints": {}}
                continue
            if result.get("continuation"):
                checkpoint["nested"] = result["continuation"]
                checkpoint["api"] = result["api"]
                transport.atomic(path, checkpoint)
                continue
            checkpoint.pop("nested", None)
            if operation == "workflow_runs":
                candidates = checkpoint.get("selections", []) + [
                    {k: row.get(k) for k in ("id", "head_branch", "status", "run_attempt")} for row in result["data"]
                ]
                unique = {str(row["id"]): row for row in candidates}
                checkpoint["selections"] = sorted(
                    unique.values(),
                    key=lambda r: (
                        r.get("head_branch") in ("staging", "trying") and r.get("status") != "completed",
                        int(r["id"]),
                    ),
                    reverse=True,
                )[:20]
                self.workflow_candidates = checkpoint["selections"]
            checkpoint["fingerprints"].update(fingerprints(operation, result["data"]))
            cursor = result.get("cursor")
            if not cursor:
                path.unlink(missing_ok=True)
                return checkpoint["fingerprints"]
            if cursor in checkpoint["seen"]:
                raise ValueError("repeated reference cursor")
            checkpoint["seen"].append(cursor)
            checkpoint.update(cursor=cursor, api=result["api"], page=checkpoint["page"] + 1)
            transport.atomic(path, checkpoint)
        return None


def compare(reference, mirror):
    return {
        "missing": len(reference.keys() - mirror.keys()),
        "extra": len(mirror.keys() - reference.keys()),
        "changed": sum(reference[k] != mirror[k] for k in reference.keys() & mirror.keys()),
    }


def update_convergence(summary, differences, checked, now):
    outstanding = summary.setdefault("outstanding", {})
    present = set()
    for row in differences:
        key = json.dumps([row["environment"], row["operation"], row.get("args", {})], sort_keys=True)
        present.add(key)
        outstanding.setdefault(key, {"first_seen": now})["last_seen"] = now
    for key in list(outstanding):
        if key in checked and key not in present:
            summary.setdefault("converged", []).append(
                {"query": key, "seconds": now - outstanding.pop(key)["first_seen"]}
            )
    summary["converged"] = summary.get("converged", [])[-1000:]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--interval", type=float, default=300)
    parser.add_argument("--state-dir", type=Path, default=Path.home() / ".local/state/tauceti/github-state-shadow")
    args = parser.parse_args()
    if args.interval < 60:
        parser.error("interval must be at least 60 seconds")
    ensure_ssl_cert_file()
    args.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    import fcntl

    lock = (args.state_dir / "sampler.lock").open("a")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise SystemExit("The comparison sampler is already running") from None
    directory = Path.home() / ".config/tauceti-github-state-test"
    repo = json.loads((directory / "installation.json").read_text())["repository"]
    path = args.state_dir / "summary.json"
    try:
        summary = json.loads(path.read_text())
    except (OSError, ValueError):
        summary = {
            "started_at": time.time(),
            "samples": 0,
            "available_samples": {},
            "reference_samples": 0,
            "reference_points": 0,
            "mismatches": 0,
        }
    while True:
        began = time.time()
        sample = {"at": began, "mirrors": {}, "differences": [], "comparisons": []}
        data = {}
        for environment, suffix in (("production", ""), ("test", "-test")):
            private = Path.home() / (".config/tauceti-github-state" + suffix)
            url = "https://github-state" + suffix + ".taucetiproject.org"
            try:
                start = time.monotonic()
                with gateway(url, private / "client-token"):
                    available, rows = read(repo, "open_prs")
                request = urllib.request.Request(
                    url + "/v1/health?repository=" + repo,
                    headers={
                        "Authorization": "Bearer " + (private / "client-token").read_text().strip(),
                        "User-Agent": "TauCetiWorker/1.0",
                    },
                )
                with urllib.request.urlopen(request, timeout=10) as response:
                    health = json.load(response)
                for quota in health["quota"].get("quota_types", []):
                    transport.quota_type(quota["category"], [json.loads(quota.get("evidence") or "{}")])
                sample["mirrors"][environment] = {
                    "available": available,
                    "query_seconds": round(time.monotonic() - start, 4),
                    "prs": len(rows) if available else None,
                    "health": health,
                }
                if available:
                    data[environment] = {p["number"]: p for p in rows}
                    summary["available_samples"][environment] = summary["available_samples"].get(environment, 0) + 1
            except Exception as error:
                sample["mirrors"][environment] = {"available": False, "error_type": type(error).__name__}
        checked = set()
        reference = Reference(directory, args.state_dir)
        try:

            def verify(
                operation, *, began=began, reference=reference, sample=sample, data=data, checked=checked, **query_args
            ):
                since = (
                    datetime.datetime.fromtimestamp(int(began / 3600) * 3600 - 3600, datetime.UTC).isoformat()
                    if operation == "workflow_runs"
                    else None
                )
                authoritative = reference.read(repo, operation, **query_args, **({"_since": since} if since else {}))
                if reference.workflow_candidates:
                    summary["workflow_candidates"] = reference.workflow_candidates
                if authoritative is None:
                    sample["comparisons"].append(
                        {"operation": operation, "args": query_args, "status": "deferred_or_paginating"}
                    )
                    return
                for environment, suffix in (("production", ""), ("test", "-test")):
                    private = Path.home() / (".config/tauceti-github-state" + suffix)
                    if operation == "open_prs":
                        available, rows = environment in data, list(data.get(environment, {}).values())
                    else:
                        with gateway("https://github-state" + suffix + ".taucetiproject.org", private / "client-token"):
                            available, rows = read(
                                repo,
                                operation,
                                _allow_observed=operation == "workflow_jobs",
                                _with_metadata=operation == "workflow_jobs",
                                **query_args,
                            )
                    if not available:
                        sample["comparisons"].append(
                            {
                                "environment": environment,
                                "operation": operation,
                                "args": query_args,
                                "status": "coverage_unavailable",
                            }
                        )
                        continue
                    if since:
                        rows = [r for r in rows if (r.get("created_at") or "") >= since.replace("+00:00", "Z")]
                    observed = operation == "workflow_jobs" and rows.get("coverage") == "webhook_observed"
                    if operation == "workflow_jobs":
                        rows = rows["data"]
                    comparison = compare(authoritative, fingerprints(operation, rows))
                    unobserved = comparison.pop("missing") if observed else None
                    if observed:
                        comparison["missing"] = 0
                    key = json.dumps([environment, operation, query_args], sort_keys=True)
                    checked.add(key)
                    item = {"environment": environment, "operation": operation, "args": query_args, **comparison}
                    if observed:
                        item.update(coverage="webhook_observed", unobserved=unobserved)
                    sample["comparisons"].append(item)
                    if any(comparison.values()):
                        sample["differences"].append(item)

            # Hourly independent inventory finds PRs missing from both mirrors.
            if began - summary.get("inventory_verified", 0) >= 3600:
                verify("open_prs")
                if any(row.get("operation") == "open_prs" and "missing" in row for row in sample["comparisons"]):
                    summary["inventory_verified"] = began
            numbers = sorted(set().union(*(set(d) for d in data.values())))
            if numbers:
                turn = summary["samples"]
                target = (
                    max(numbers, key=lambda n: max((d.get(n, {}).get("updatedAt", "") or "") for d in data.values()))
                    if turn % 2 == 0
                    else numbers[(turn // 2) % len(numbers)]
                )
                operations = (
                    "pr",
                    "issue_comments",
                    "review_comments",
                    "review_threads",
                    "reactions",
                    "statuses",
                    "checks",
                    "workflow_runs",
                    "workflow_jobs",
                )
                operation = operations[turn % len(operations)]
                if operation in ("statuses", "checks"):
                    sha = next(d[target]["headRefOid"] for d in data.values() if target in d)
                    verify(operation, sha=sha)
                elif operation in ("workflow_runs", "workflow_jobs"):
                    if operation == "workflow_runs":
                        verify(operation)
                    elif summary.get("workflow_candidates"):
                        runs = summary["workflow_candidates"]
                        important = [
                            r
                            for r in runs
                            if r.get("head_branch") in ("staging", "trying") and r.get("status") != "completed"
                        ]
                        chosen = (important or runs)[turn % len(important or runs)]
                        verify(operation, id=str(chosen["id"]))
                elif operation == "reactions":
                    with gateway("https://github-state-test.taucetiproject.org", directory / "client-token"):
                        available, comments = read(repo, "review_comments", number=target)
                    if available and comments:
                        verify(operation, id=str(comments[turn % len(comments)]["id"]))
                else:
                    verify(operation, number=target)
        except Exception as error:
            sample["reference_error_type"] = type(error).__name__
        sample.update(
            reference_points=reference.points,
            reference_requests=reference.requests,
            reference_local_deferrals=reference.local_deferrals,
            reference_deferred=reference.deferred,
        )
        if checked:
            summary["reference_samples"] += 1
        summary["reference_points"] += reference.points
        update_convergence(summary, sample["differences"], checked, began)
        summary["samples"] += 1
        summary["mismatches"] += len(sample["differences"])
        summary["last_sample"] = sample
        summary["elapsed_hours"] = (time.time() - summary["started_at"]) / 3600
        summary["status"] = "observing; comparisons do not gate model spending"
        with (args.state_dir / "samples.jsonl").open("a") as stream:
            stream.write(json.dumps(sample) + "\n")
        (args.state_dir / "samples.jsonl").chmod(0o600)
        transport.atomic(path, summary)
        print(
            json.dumps(
                {
                    "sample": summary["samples"],
                    "available": list(data),
                    "differences": len(sample["differences"]),
                    "reference_requests": reference.requests,
                }
            ),
            flush=True,
        )
        if args.once:
            return
        time.sleep(max(1, args.interval - (time.time() - began)))


if __name__ == "__main__":
    main()

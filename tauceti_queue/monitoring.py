"""Prepared public queue observations. The publisher never writes the controller's database.

Run as an independent sidecar with ``python -m tauceti_queue.monitoring``. Only the
allowlisted projection leaves this process; neither HTTP viewers nor publication failures
run a controller operation, inspect GitHub, or change admissions.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import signal
import sqlite3
import subprocess
import threading
import time
import urllib.request
import uuid
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

SCHEMA = "tauceti.operations.queue/v1"
LIMIT = 20_000
REPO = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")
SHA = re.compile(r"[0-9a-f]{40}\Z")
MAX_BYTES = 2_000_000


def iso(value):
    return datetime.fromtimestamp(value, UTC).isoformat().replace("+00:00", "Z") if value is not None else None


def text(value, limit=300):
    return str(value or "")[:limit]


def numeric(value):
    return value if type(value) in (int, float) and math.isfinite(value) and value >= 0 else None


def json_read(path, limit=MAX_BYTES):
    with Path(path).open("rb") as stream:
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise ValueError("observation exceeds limit")
    return json.loads(data)


def public_url(repo, kind, identity):
    if not REPO.fullmatch(repo):
        raise ValueError("invalid repository")
    if kind == "commit" and not SHA.fullmatch(str(identity or "")):
        return None
    if kind in ("pull", "actions/runs") and not str(identity or "").isdigit():
        return None
    return f"https://github.com/{repo}/{kind}/{identity}"


class CommitFacts:
    """Cache immutable commit metadata; bounded batch reads happen only in the sidecar."""

    def __init__(self, clone):
        self.clone = clone
        self.cache = {}

    def _read(self, shas):
        requested = sorted(s for s in shas if SHA.fullmatch(s))[:2000]
        if not requested:
            return {}
        args = ["git", "-C", str(self.clone), "cat-file"]
        checked = subprocess.run(
            [*args, "--batch-check"],
            input="".join(s + "\n" for s in requested),
            text=True,
            capture_output=True,
            check=True,
            timeout=15,
        )
        selected, size = [], 0
        for line in checked.stdout.splitlines():
            fields = line.split()
            if len(fields) != 3 or fields[1] != "commit" or not fields[2].isdigit():
                continue
            length = int(fields[2])
            if length <= 65536 and size + length <= 8_000_000:
                selected.append(fields[0])
                size += length
        result = subprocess.run(
            [*args, "--batch"],
            input="".join(s + "\n" for s in selected).encode(),
            capture_output=True,
            check=True,
            timeout=15,
        )
        data, offset, records = result.stdout, 0, {}
        while offset < len(data):
            end = data.index(b"\n", offset)
            header = data[offset:end].decode().split()
            offset = end + 1
            if len(header) != 3:
                continue
            length = int(header[2])
            body = data[offset : offset + length].decode(errors="replace")
            offset += length + 1
            headers, _, message = body.partition("\n\n")
            records[header[0]] = {
                "tree": next((s[5:] for s in headers.splitlines() if s.startswith("tree ")), None),
                "parent": next((s[7:] for s in headers.splitlines() if s.startswith("parent ")), None),
                "already_landed": bool(
                    re.search(
                        r"^Landed on main as [0-9a-f]{40} outside the queue; nothing left to apply\.$", message, re.M
                    )
                ),
            }
        return records

    def observe(self, members):
        if self.clone is None:
            return self.cache
        missing = {m["commit_sha"] for m in members if m["commit_sha"] and m["commit_sha"] not in self.cache}
        try:
            commits = self._read(missing)
            parents = self._read({v["parent"] for v in commits.values() if v["parent"]})
        except (OSError, ValueError, subprocess.SubprocessError):
            return self.cache  # Unknown remains retryable on the next pass.
        for sha, value in commits.items():
            parent = parents.get(value["parent"])
            if parent is not None:
                self.cache[sha] = {"empty": value["tree"] == parent["tree"], "already_landed": value["already_landed"]}
        return self.cache


def workers(roots):
    result = []
    for index, root in enumerate(roots):
        try:
            value = json_read(Path(root) / "worker.json")
            result.append(
                {
                    "id": text(value.get("worker")),
                    "state": text(value.get("state"), 40),
                    "observed_at": iso(numeric(value.get("updated_at"))),
                    "started_at": iso(numeric(value.get("started_at"))),
                    "tasks": numeric(value.get("tasks")),
                    "errors": numeric(value.get("errors")),
                    # Never copy last_error, root, environment, private paths or command lines.
                    "coverage": "observed",
                }
            )
        except (OSError, ValueError, TypeError):
            result.append({"id": f"worker-{index + 1}", "coverage": "unavailable", "observed_at": None})
    return result


def project(
    database,
    *,
    status=None,
    worker_roots=(),
    facts=None,
    public_repositories=("TauCetiProject/TauCeti",),
    clock=time.time,
):
    """One read-only SQLite snapshot; fixed field lists exclude claims' tokens and raw tasks."""
    status = status or {}
    public_repositories = {r for r in public_repositories if REPO.fullmatch(r)}
    with closing(sqlite3.connect(Path(database).resolve().as_uri() + "?mode=ro", uri=True)) as conn, conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        conn.execute("BEGIN")

        def rows(sql, params=()):
            return [dict(r) for r in conn.execute(sql, params)]

        batches = rows(
            "SELECT id,repository,version,base,tip,state,created_at,sealed_at,landed_at,"
            "first_dispatch_at,pre_rebase_state,pre_rebase_tip,pre_rebase_onto,pre_rebase_prepared "
            "FROM batches ORDER BY CASE state WHEN 'head' THEN 0 WHEN 'open' THEN 1 ELSE 2 END,id DESC LIMIT ?",
            (LIMIT + 1,),
        )
        members = rows(
            "SELECT id,batch_id,input_id,number,accepted_head,position,commit_sha,provenance,"
            "task_id,explanation,added_at,removed,removed_reason,no_repair,withdrawn FROM members "
            "ORDER BY id DESC LIMIT ?",
            (LIMIT + 1,),
        )
        inputs = rows(
            "SELECT id,repository,number,head_sha,title,author,state,approved_at,batch_id,"
            "landed_at,landed_sha,no_repair FROM inputs ORDER BY id DESC LIMIT ?",
            (LIMIT + 1,),
        )
        attempts = rows(
            "SELECT id,batch_id,batch_version,head_sha,base_sha,dispatched_at,run_id,run_attempt,"
            "run_status,run_conclusion,run_started_at,run_completed_at,runner_name,runner_labels,"
            "host,phases,last_progress_at,last_observed_at,outcome,verdicts,cancelled_at "
            "FROM ci_attempts ORDER BY id DESC LIMIT ?",
            (LIMIT + 1,),
        )
        tasks = rows(
            "SELECT id,kind,batch_id,batch_version,state,created_at,resolved_at,resolution,"
            "ci_attempt_id FROM tasks ORDER BY id DESC LIMIT ?",
            (LIMIT + 1,),
        )
        claims = rows(
            "SELECT batch_id,task_id,operation,worker,created_at,expires_at FROM claims "
            "WHERE released_at IS NULL ORDER BY id DESC LIMIT 1001"
        )
        events = rows("SELECT id,at,kind,batch_id FROM journal ORDER BY id DESC LIMIT 3001")
        outbox = rows("SELECT kind,state,COUNT(*) count FROM outbox GROUP BY kind,state")
        counts = {
            str(r["batch_id"]): r["size"]
            for r in rows("SELECT batch_id,COUNT(*) size FROM members WHERE removed=0 GROUP BY batch_id")
        }
        # Future explicit reduction events can identify a cause. Historical emptiness alone cannot.
        reductions = rows(
            "SELECT detail FROM journal WHERE kind='member-reduced-to-empty' ORDER BY id DESC LIMIT ?", (LIMIT,)
        )
        captured = clock()
    tables = (batches, members, inputs, attempts, tasks)
    truncated = any(len(t) > LIMIT for t in tables)
    for table in tables:
        del table[LIMIT:]
    batches = [r for r in batches if r["repository"] in public_repositories]
    inputs = [r for r in inputs if r["repository"] in public_repositories]
    known = facts.observe(members) if facts else {}
    reduced = set()
    for event in reductions:
        detail = json.loads(event["detail"])
        if isinstance(detail.get("commit"), str):
            reduced.add(detail["commit"])
    input_map = {r["id"]: r for r in inputs}
    batch_map = {r["id"]: r for r in batches}
    member_records = []
    for row in members:
        item = input_map.get(row["input_id"], {})
        batch = batch_map.get(row["batch_id"], {})
        repo = batch.get("repository") or item.get("repository")
        if not repo or not REPO.fullmatch(repo):
            continue
        commit = known.get(row["commit_sha"], {})
        disposition = "unknown"
        if commit.get("empty") is True:
            disposition = (
                "already_landed_empty"
                if commit.get("already_landed")
                else ("reduced_empty" if row["commit_sha"] in reduced else "empty_unknown")
            )
        elif commit.get("empty") is False:
            disposition = "agent_edited" if row["provenance"] == "edited" else "clean_replay"
        member_records.append(
            {
                "id": str(row["id"]),
                "repository": repo,
                "pr": str(row["number"]),
                "batch_id": str(row["batch_id"]),
                "position": row["position"],
                "accepted_head": row["accepted_head"],
                "commit": row["commit_sha"],
                "title": text(item.get("title")),
                "author": text(item.get("author")),
                "approved_at": iso(item.get("approved_at")),
                "inserted_at": iso(row["added_at"]),
                "landed_at": iso(batch.get("landed_at")),
                "provenance": row["provenance"],
                "disposition": disposition,
                "empty": commit.get("empty"),
                "removed": bool(row["removed"]),
                "no_repair": bool(row["no_repair"]),
                "withdrawn": bool(row["withdrawn"]),
                "explanation": text(row["explanation"], 2000),
                "pr_url": public_url(repo, "pull", row["number"]),
                "commit_url": public_url(repo, "commit", row["commit_sha"]),
            }
        )
    attempt_records = []
    for row in attempts:
        repo = batch_map.get(row["batch_id"], {}).get("repository")
        if not repo:
            continue
        verdicts = json.loads(row["verdicts"])
        attempt_records.append(
            {
                "id": str(row["id"]),
                "batch_id": str(row["batch_id"]),
                "version": row["batch_version"],
                "candidate": row["head_sha"],
                "base": row["base_sha"],
                "run_id": str(row["run_id"]) if row["run_id"] else None,
                "run_attempt": row["run_attempt"],
                "status": row["run_status"],
                "conclusion": row["run_conclusion"],
                "outcome": row["outcome"],
                "dispatched_at": iso(row["dispatched_at"]),
                "started_at": iso(row["run_started_at"]),
                "completed_at": iso(row["run_completed_at"]),
                "cancelled_at": iso(row["cancelled_at"]),
                "last_progress_at": iso(row["last_progress_at"]),
                "observed_at": iso(row["last_observed_at"]),
                "runner": text(row["runner_name"]),
                "labels": [text(x, 100) for x in json.loads(row["runner_labels"])[:30]],
                "url": public_url(repo, "actions/runs", row["run_id"]),
                "verdicts": {
                    k: {"state": text(v.get("state"), 30), "bound": v.get("bound") is True}
                    for k, v in verdicts.items()
                    if k in ("build", "scope", "bump-guard")
                },
                "phases": [
                    {
                        "name": text(p.get("name")),
                        "status": text(p.get("status"), 30),
                        "started_at": iso(p.get("started_at")),
                        "completed_at": iso(p.get("completed_at")),
                    }
                    for p in json.loads(row["phases"])[:100]
                ],
            }
        )
    public_batches = []
    for row in batches:
        if not REPO.fullmatch(row["repository"]):
            continue
        public_batches.append(
            {
                "id": str(row["id"]),
                "repository": row["repository"],
                "version": row["version"],
                "base": row["base"],
                "tip": row["tip"],
                "state": row["state"],
                "size": counts.get(str(row["id"]), 0),
                **{k: iso(row[k]) for k in ("created_at", "sealed_at", "landed_at", "first_dispatch_at")},
                "pre_rebase_state": row["pre_rebase_state"],
                "pre_rebase_tip": row["pre_rebase_tip"],
                "pre_rebase_onto": row["pre_rebase_onto"],
                "prepared_tip": row["pre_rebase_prepared"],
            }
        )
    service = status.get("service") or {}
    head = status.get("head") or {}
    hold = status.get("inference_hold") or {}
    conditions = []
    if head.get("stall"):
        conditions.append(
            {
                "kind": "build_silence",
                "since": iso(head["stall"].get("since")),
                "batch_id": str(head["id"]),
                "seconds": numeric(head["stall"].get("silence_seconds")),
            }
        )
    if hold:
        conditions.append({"kind": "inference_hold", "since": iso(hold.get("since"))})
    for alert in status.get("alerts") or []:
        kind = (
            "promotion_failed"
            if str(alert).startswith("promotion is failing")
            else "chain_invalid"
            if "submitted chain failed validation" in str(alert)
            else "source_condition"
        )
        if kind != "source_condition" or not any(x in str(alert) for x in ("build stalled", "inference hold")):
            conditions.append({"kind": kind})
    return {
        "schema": SCHEMA,
        "observed_at": iso(captured),
        "source_revision": text(service.get("revision"), 40),
        "coverage": "partial" if truncated else "observed",
        "truncated": truncated,
        "scope": {"repositories": sorted(public_repositories)},
        "service": {
            "revision": text(service.get("revision"), 40),
            "started_at": text(service.get("started_at"), 40),
            "mode": service.get("mode") if service.get("mode") in ("production", "shadow") else "unknown",
            "controller": service.get("controller")
            if service.get("controller") in ("running", "unavailable")
            else "unknown",
            "publication_enabled": service.get("publication_enabled")
            if type(service.get("publication_enabled")) is bool
            else None,
        },
        "status_observed_at": text(status.get("generated_at"), 40) or None,
        "conditions": conditions,
        "outbox": [{"kind": text(r["kind"], 60), "state": text(r["state"], 30), "count": r["count"]} for r in outbox],
        "settings": {
            k: v
            for k, v in (status.get("settings") or {}).items()
            if k
            in (
                "repair_concurrency",
                "stall_interval_seconds",
                "open_batch_limit",
                "prepare_min_members",
                "prepare_quiet_seconds",
                "prepare_open_batches",
                "pre_rebase_enabled",
                "prepare_pre_rebased",
            )
            and (type(v) is bool or numeric(v) is not None)
        },
        "batches": public_batches,
        "members": member_records,
        "attempts": attempt_records,
        "waiting": [
            {
                "repository": r["repository"],
                "pr": str(r["number"]),
                "accepted_head": r["head_sha"],
                "title": text(r["title"]),
                "approved_at": iso(r["approved_at"]),
            }
            for r in inputs
            if r["state"] in ("waiting", "reserved") and REPO.fullmatch(r["repository"])
        ],
        "tasks": [
            {
                **{
                    k: str(r[k]) if k in ("id", "batch_id", "ci_attempt_id") and r[k] is not None else r[k]
                    for k in ("id", "kind", "batch_id", "batch_version", "state", "resolution", "ci_attempt_id")
                },
                "created_at": iso(r["created_at"]),
                "resolved_at": iso(r["resolved_at"]),
            }
            for r in tasks
            if r["batch_id"] in batch_map
        ],
        "claims": [
            {
                "batch_id": str(r["batch_id"]),
                "task_id": str(r["task_id"]) if r["task_id"] else None,
                "operation": text(r["operation"], 40),
                "worker": text(r["worker"]),
                "created_at": iso(r["created_at"]),
                "expires_at": iso(r["expires_at"]),
            }
            for r in claims[:1000]
            if r["batch_id"] in batch_map
        ],
        "events": [
            {
                "id": str(r["id"]),
                "at": iso(r["at"]),
                "kind": text(r["kind"], 80),
                "batch_id": str(r["batch_id"]) if r["batch_id"] is not None else None,
            }
            for r in events[:3000]
            if r["batch_id"] in batch_map
        ],
        "event_history_truncated": len(events) > 3000,
        "claim_history_truncated": len(claims) > 1000,
        "workers": workers(worker_roots),
    }


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # Never forward the machine's publication credential to a redirect target.


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--clone", type=Path)
    parser.add_argument("--worker-root", type=Path, action="append", default=[])
    parser.add_argument("--public-repository", action="append", default=[])
    parser.add_argument("--status-url", default="http://127.0.0.1:8780/status")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--publish-url")
    parser.add_argument("--token-file", type=Path)
    parser.add_argument("--interval", type=float, default=10)
    parser.add_argument("--follow", action="store_true")
    args = parser.parse_args(argv)
    if args.interval < 5 or args.interval > 300:
        parser.error("interval must be between 5 and 300 seconds")
    if args.publish_url and (not args.token_file or not args.publish_url.startswith("https://")):
        parser.error("publication requires an HTTPS endpoint and --token-file")
    stop, boot, started, sequence = threading.Event(), str(uuid.uuid4()), iso(time.time()), 0
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    facts = CommitFacts(args.clone)
    opener = urllib.request.build_opener(NoRedirect)
    while not stop.is_set():
        try:
            try:
                with urllib.request.urlopen(args.status_url, timeout=3) as response:
                    status = json.loads(response.read(MAX_BYTES))
            except (OSError, ValueError):
                status = {}
            observation = project(
                args.database,
                status=status,
                worker_roots=args.worker_root,
                facts=facts,
                public_repositories=args.public_repository or ("TauCetiProject/TauCeti",),
            )
            sequence += 1
            observation["source"] = {"boot_id": boot, "started_at": started, "sequence": sequence}
            payload = json.dumps(observation, separators=(",", ":")).encode()
            if len(payload) > MAX_BYTES:
                raise ValueError("public observation exceeds limit")
            if args.output:
                args.output.parent.mkdir(parents=True, exist_ok=True)
                temporary = args.output.with_suffix(".tmp")
                temporary.write_bytes(payload)
                os.replace(temporary, args.output)
            if args.publish_url:
                token = args.token_file.read_text().strip()
                request = urllib.request.Request(
                    args.publish_url,
                    data=payload,
                    method="POST",
                    headers={
                        "Authorization": "Bearer " + token,
                        "Content-Type": "application/json",
                        "User-Agent": "tauceti-queue-monitor/1",
                    },
                )
                with opener.open(request, timeout=10) as response:
                    if response.status not in (200, 202, 204):
                        raise ValueError("publication rejected")
            elif not args.output:
                print(payload.decode(), flush=True)
        except (OSError, ValueError, sqlite3.Error) as error:
            # Exception text may contain private paths or request URLs. It is never telemetry.
            print(
                f"queue monitoring: {type(error).__name__}; retrying"
                if args.follow
                else f"queue monitoring: {type(error).__name__}",
                file=__import__("sys").stderr,
            )
            if not args.follow:
                return 1
        if not args.follow or stop.wait(args.interval):
            break
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

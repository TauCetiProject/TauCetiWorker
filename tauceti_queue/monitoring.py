"""Prepared public queue observations. The publisher never writes the controller's database.

Run as an independent sidecar with ``python -m tauceti_queue.monitoring``. Only the
allowlisted projection leaves this process; neither HTTP viewers nor publication failures
run a controller operation, inspect GitHub, or change admissions.
"""

from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import math
import os
import re
import signal
import sqlite3
import stat
import subprocess
import sys
import threading
import time
import tomllib
import urllib.error
import urllib.request
from contextlib import ExitStack, closing
from datetime import UTC, datetime
from pathlib import Path

from . import task_progress
from .monitoring_protocol import PublicationRejected, PublisherIdentity, ResyncRequired, SnapshotPublisher, encode

SCHEMA = "tauceti.operations.queue/v1"
LIMIT = 20_000
REPO = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")
SHA = re.compile(r"[0-9a-f]{40}\Z")
MAX_BYTES = 2_000_000
BUILD_TAIL_BYTES = 64_000


def iso(value):
    return datetime.fromtimestamp(value, UTC).isoformat().replace("+00:00", "Z") if value is not None else None


def text(value, limit=300):
    if not isinstance(value, str):
        return ""  # Never stringify an unexpected private object in a display field.
    return re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", value)[:limit]


def worker_alias(value):
    return "queue-worker-" + hashlib.sha256((value if isinstance(value, str) else "unknown").encode()).hexdigest()[:12]


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
        self.policy_cache = {}

    def dispatch_config(self, revision):
        """Read only allowlisted policy from an immutable, already-local commit."""
        if self.clone is None or not isinstance(revision, str) or not SHA.fullmatch(revision):
            return None
        if revision in self.policy_cache:
            return self.policy_cache[revision]
        args = ["git", "-C", str(self.clone), "cat-file"]
        env = dict(os.environ, GIT_NO_LAZY_FETCH="1")
        spec = revision + ":tauceti.toml"
        try:
            size = subprocess.run([*args, "-s", spec], capture_output=True, check=True, timeout=1, env=env)
            if not size.stdout.strip().isdigit() or not 0 < int(size.stdout) <= 65536:
                return None
            result = subprocess.run([*args, "blob", spec], capture_output=True, check=True, timeout=1, env=env)
            config = tomllib.loads(result.stdout.decode())["services"]["fleet"]
            cap = config["namespace_build_cap"]
            if type(cap) is not int or not 0 <= cap <= 10000:
                return None
            for order in (config["pr_order"], config["staging_order"]):
                if (
                    not isinstance(order, list)
                    or not 0 < len(order) <= 20
                    or any(
                        not isinstance(name, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,59}", name) for name in order
                    )
                    or len(set(order)) != len(order)
                ):
                    return None
            value = {
                "repository": "TauCetiProject/TauCeti",
                "revision": revision,
                "namespace_build_cap": cap,
                "pr_order": config["pr_order"],
                "staging_order": config["staging_order"],
            }
        except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
            return None
        self.policy_cache[revision] = value
        if len(self.policy_cache) > 128:
            del self.policy_cache[next(iter(self.policy_cache))]
        return value

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


def verified_worker_alive(value, root, *, proc=Path("/proc")):
    """Positive local liveness evidence only; False means unverified, never dead."""
    pid, started = value.get("pid"), numeric(value.get("started_at"))
    if type(pid) is not int or not 0 < pid < 2**31 or started is None:
        return False
    try:
        if not isinstance(value.get("root"), str) or Path(value["root"]).resolve() != Path(root).resolve():
            return False
        process = proc / str(pid)
        if process.stat().st_uid != os.getuid():
            return False
        with (process / "cmdline").open("rb") as stream:
            command = stream.read(65_537)
        if len(command) > 65_536 or b"tauceti_queue.repair_task" not in command.split(b"\0"):
            return False
        with (process / "stat").open() as stream:
            record = stream.read(4097)
        if len(record) > 4096:
            return False
        ticks = int(record[record.rfind(")") + 2 :].split()[19])
        if ticks < 0:
            return False
        born = time.time() - time.clock_gettime(time.CLOCK_BOOTTIME) + ticks / os.sysconf("SC_CLK_TCK")
        # The process must predate this worker session. An unrelated/reused PID
        # or inaccessible evidence leaves the old checkpoint visibly dated.
        return born <= started <= time.time() + 5
    except (OSError, ValueError, IndexError, AttributeError):
        return False


def workers(roots, *, claims=(), task_states=None, captured=None):
    captured = time.time() if captured is None else captured
    task_states = task_states or {}
    result = []
    for index, root in enumerate(roots):
        try:
            value = json_read(Path(root) / "worker.json")
            alias = worker_alias(value.get("worker"))
            alive = verified_worker_alive(value, root)
            active = [c for c in claims if worker_alias(c.get("worker")) == alias and c.get("expires_at", 0) > captured]
            phase = next((p for c in active if (p := task_states.get(c.get("task_id"), {}).get("state"))), None)
            state = text(value.get("state"), 40)
            if alive:
                checkpoint_age = captured - (numeric(value.get("updated_at")) or 0)
                if phase:
                    state = text("alive / " + phase, 40)
                elif active or not -5 <= checkpoint_age <= 60:
                    state = "alive; task state unknown"
                else:
                    state = text("alive / " + state, 40)
            result.append(
                {
                    "id": alias,
                    "state": state,
                    "observed_at": iso(captured if alive else numeric(value.get("updated_at"))),
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


class BuildObservations:
    """Numeric observations of existing local build logs, never log excerpts or verdicts."""

    def __init__(self):
        self.cache = {}

    def observe(self, directory, phase, token, captured, phase_at):
        stage, name = {
            "sources_ready": ("preparation", "prepare-build.log"),
            "verifying": ("verification", "verify-build.log"),
            "agent_running": ("streaming", "stream-build.log"),
        }.get(phase, (None, None))
        if name is None:
            return None
        try:
            descriptor = os.open(directory / "logs" / name, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
            with os.fdopen(descriptor, "rb") as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode) or (phase_at is not None and info.st_mtime < phase_at):
                    return None
                offset = max(0, info.st_size - BUILD_TAIL_BYTES)
                stream.seek(offset)
                tail = stream.read(BUILD_TAIL_BYTES)
                if offset:
                    tail = tail.partition(b"\n")[2]  # Never parse a truncated line as a counter.
            matches = re.findall(rb"\[(\d{1,9})/(\d{1,9})\]", tail)
            valid = [(int(a), int(b)) for a, b in matches if 0 <= int(a) <= int(b) and int(b) > 0]
            if not valid:
                return None
            completed, total = valid[-1]
            identity = (info.st_dev, info.st_ino, token, phase_at)
            key = (str(directory), stage)
            previous = self.cache.get(key)
            last_progress = None
            if previous and previous[0] == identity and info.st_size >= previous[1] and completed >= previous[2]:
                last_progress = captured if completed > previous[2] else previous[3]
            self.cache[key] = (identity, info.st_size, completed, last_progress)
            if len(self.cache) > 1000:
                self.cache = dict(list(self.cache.items())[-500:])
            return {
                "stage": stage,
                "completed_tasks": completed,
                "total_tasks": total,
                "sampled_at": iso(captured),
                "log_updated_at": iso(info.st_mtime),
                "last_progress_at": iso(last_progress),
            }
        except (OSError, ValueError, OverflowError):
            return None


class TaskObservations:
    """Bounded cached projection of trusted worker checkpoints/outcomes."""

    def __init__(self):
        self.cache = {}
        self.builds = BuildObservations()

    def observe(self, roots, tasks, *, claims=(), observed_at=None):
        result = {}
        observed_at = time.time() if observed_at is None else observed_at
        for task in sorted(tasks, key=lambda t: (t["state"] != "open", -t["id"]))[:100]:
            candidates = []
            for root in roots:
                directory = Path(root) / "tasks" / str(task["id"])
                try:
                    progress = directory / task_progress.FILENAME
                    try:
                        progress_stat = progress.stat()
                    except FileNotFoundError:
                        progress_stat = None
                    checkpoint = directory / "task.json"
                    outcome = directory / "outcome.json"
                    fingerprint = (
                        ("progress", progress_stat.st_mtime_ns, progress_stat.st_ino)
                        if progress_stat is not None
                        else (
                            "legacy",
                            checkpoint.stat().st_mtime_ns,
                            outcome.stat().st_mtime_ns if outcome.exists() else None,
                        )
                    )
                    key = str(directory)
                    if self.cache.get(key, (None,))[0] != fingerprint:
                        if progress_stat is not None:
                            record = json_read(progress)
                        else:
                            # Trusted legacy files have no task-size ceiling. This compatibility
                            # path uses memory proportional to their size; new workers avoid it.
                            with checkpoint.open() as stream:
                                raw = json.load(stream)
                            if outcome.exists():
                                with outcome.open() as stream:
                                    final = json.load(stream)
                            else:
                                final = raw.get("outcome") or raw
                            record = task_progress.summarize(raw, final)
                        if str(record.get("task_id")) != str(task["id"]):
                            continue
                        observation = task_progress.public(record)
                        binding = record.get("claim_token")
                        self.cache[key] = (fingerprint, observation, binding if isinstance(binding, str) else None)
                    candidates.append((*self.cache[key][1:], directory))
                except (OSError, ValueError, TypeError, AttributeError):
                    continue
            if candidates:
                # Parallel workers may release one attempt while another still
                # holds this task. Tokens bind checkpoints to the current lease
                # locally; only the sanitized observation leaves the exporter.
                live_tokens = (
                    {
                        claim["token"]
                        for claim in claims
                        if claim.get("task_id") == task["id"] and claim.get("expires_at", 0) > observed_at
                    }
                    if task["state"] == "open"
                    else set()
                )
                selected = max(candidates, key=lambda item: (item[1] in live_tokens, item[0]["observed_at"] or ""))
                observation, token, directory = selected
                phase_at = (
                    datetime.fromisoformat(observation["observed_at"]).timestamp()
                    if observation["observed_at"]
                    else None
                )
                build = (
                    self.builds.observe(directory, observation["state"], token, observed_at, phase_at)
                    if task["state"] == "open"
                    else None
                )
                result[task["id"]] = observation | ({"build": build} if build else {})
        if len(self.cache) > 1000:
            self.cache = dict(list(self.cache.items())[-500:])
        return result


def project(
    database,
    *,
    status=None,
    worker_roots=(),
    facts=None,
    task_observations=None,
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
            "landed_at,landed_sha,no_repair FROM inputs ORDER BY CASE WHEN state IN ('waiting','reserved') THEN 0 ELSE 1 END,id DESC LIMIT ?",
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
            "SELECT batch_id,task_id,operation,worker,created_at,expires_at,token FROM claims "
            "WHERE released_at IS NULL ORDER BY id DESC LIMIT 1001"
        )
        events = rows("SELECT id,at,kind,batch_id FROM journal ORDER BY id DESC LIMIT 3001")
        observed_repositories = {
            r["repository"]
            for r in rows("SELECT DISTINCT repository FROM batches UNION SELECT DISTINCT repository FROM inputs")
        }
        outbox = (
            rows("SELECT kind,state,COUNT(*) count FROM outbox GROUP BY kind,state")
            if observed_repositories <= public_repositories
            else None
        )
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
    complete = {
        k: len(v) <= LIMIT for k, v in zip(("batches", "members", "inputs", "attempts", "tasks"), tables, strict=True)
    }
    complete["events"] = len(events) <= 3000
    for table in tables:
        del table[LIMIT:]
    # Keep active work and a bounded 90-day terminal window. Historic totals
    # belong to the journal report, rather than an ever-growing live projection.
    terminal = [
        r
        for r in batches
        if r["state"] not in ("head", "open") and (r["landed_at"] or r["created_at"]) >= captured - 90 * 86400
    ][:500]
    batches = [
        r
        for r in batches
        if (r["state"] in ("head", "open") or r in terminal) and r["repository"] in public_repositories
    ]
    inputs = [r for r in inputs if r["repository"] in public_repositories]
    live_inputs = [r for r in inputs if r["state"] in ("waiting", "reserved")]
    inputs = live_inputs + [r for r in inputs if r["state"] not in ("waiting", "reserved")][:5000]
    known = facts.observe(members) if facts else {}
    reduced = set()
    for event in reductions:
        detail = json.loads(event["detail"])
        if isinstance(detail.get("commit"), str):
            reduced.add(detail["commit"])
    input_map = {r["id"]: r for r in inputs}
    batch_map = {r["id"]: r for r in batches}
    task_progress = (
        task_observations.observe(
            worker_roots, [t for t in tasks if t["batch_id"] in batch_map], claims=claims, observed_at=captured
        )
        if task_observations
        else {}
    )
    member_records = []
    for row in members:
        item = input_map.get(row["input_id"], {})
        batch = batch_map.get(row["batch_id"], {})
        repo = batch.get("repository")
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
                "batch_version": batch["version"],
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
    if head.get("stall") and head.get("id") in batch_map:
        conditions.append(
            {
                "kind": "task_unclaimed" if head["stall"].get("kind") == "task_unclaimed" else "build_silence",
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
        if kind != "source_condition" or not any(
            x in str(alert) for x in ("build stalled", "inference hold", " unclaimed for ")
        ):
            conditions.append({"kind": kind})
    observation = {
        "schema": SCHEMA,
        "observed_at": iso(captured),
        "source_revision": text(service.get("revision"), 40),
        "canonical_sha": status.get("canonical_sha")
        if isinstance(status.get("canonical_sha"), str) and SHA.fullmatch(status["canonical_sha"])
        else None,
        "dispatch_config": facts.dispatch_config(status.get("canonical_sha"))
        if facts and "TauCetiProject/TauCeti" in public_repositories and hasattr(facts, "dispatch_config")
        else None,
        "coverage": "partial" if truncated else "observed",
        "truncated": truncated,
        "collection_complete": complete,
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
        "status_observed_at": iso(numeric(service.get("now"))) or text(status.get("generated_at"), 40) or None,
        "conditions": conditions,
        "outbox": [{"kind": text(r["kind"], 60), "state": text(r["state"], 30), "count": r["count"]} for r in outbox]
        if outbox is not None
        else None,
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
        "inputs": [
            {
                "id": str(r["id"]),
                "repository": r["repository"],
                "pr": str(r["number"]),
                "accepted_head": r["head_sha"],
                "state": r["state"],
                "batch_id": str(r["batch_id"]) if r["batch_id"] is not None else None,
                "title": text(r["title"]),
                "approved_at": iso(r["approved_at"]),
                "landed_at": iso(r["landed_at"]),
                "landed_commit": r["landed_sha"],
                "no_repair": bool(r["no_repair"]),
            }
            for r in inputs
        ],
        "tasks": [
            {
                **{
                    k: str(r[k]) if k in ("id", "batch_id", "ci_attempt_id") and r[k] is not None else r[k]
                    for k in ("id", "kind", "batch_id", "batch_version", "state", "resolution", "ci_attempt_id")
                },
                "created_at": iso(r["created_at"]),
                "resolved_at": iso(r["resolved_at"]),
                "progress": task_progress.get(r["id"], {"coverage": "unavailable", "observed_at": None}),
            }
            for r in tasks
            if r["batch_id"] in batch_map
        ],
        "claims": [
            {
                "batch_id": str(r["batch_id"]),
                "task_id": str(r["task_id"]) if r["task_id"] else None,
                "operation": text(r["operation"], 40),
                "worker": worker_alias(r["worker"]),
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
        "workers": workers(worker_roots, claims=claims, task_states=task_progress, captured=captured),
    }

    # Full live projection stays comfortably inside the receiver's 16MB budget.
    # Extraordinary active-only overflow is explicitly partial, never an admission change.
    while len(encode(observation)) > 12_000_000:
        terminal = [b for b in observation["batches"] if b["state"] not in ("head", "open")]
        if terminal:
            drop = min(terminal, key=lambda b: int(b["id"]))["id"]
            observation["batches"] = [b for b in observation["batches"] if b["id"] != drop]
            for name in ("members", "attempts", "tasks", "events"):
                observation[name] = [r for r in observation[name] if r["batch_id"] != drop]
        else:
            name = max(
                (k for k in ("members", "inputs", "attempts", "tasks", "events") if observation[k]),
                key=lambda k: len(encode(observation[k])),
                default=None,
            )
            if name is None:
                raise ValueError("metadata exceeds public projection budget")
            observation[name] = observation[name][: max(0, len(observation[name]) - 100)]
            observation["collection_complete"][name] = False
        observation["truncated"] = True
        observation["coverage"] = "partial"
    return observation


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
    parser.add_argument("--state-file", type=Path, help="persistent publisher identity and single-process lock")
    parser.add_argument(
        "--recover-state",
        action="store_true",
        help="explicitly recover this queue's publisher identity from its authenticated receiver",
    )
    parser.add_argument("--interval", type=float, default=10)
    parser.add_argument("--follow", action="store_true")
    args = parser.parse_args(argv)
    if args.interval < 5 or args.interval > 300:
        parser.error("interval must be between 5 and 300 seconds")
    if args.publish_url and (not args.token_file or not args.state_file or not args.publish_url.startswith("https://")):
        parser.error("publication requires an HTTPS endpoint, --token-file and --state-file")
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    facts = CommitFacts(args.clone)
    opener = urllib.request.build_opener(NoRedirect)
    if args.recover_state:
        if not args.publish_url or not args.state_file:
            parser.error("state recovery requires the publication endpoint and private state file")
        request = urllib.request.Request(
            args.publish_url.rstrip("/") + "/state",
            headers={
                "Authorization": "Bearer " + args.token_file.read_text().strip(),
                "User-Agent": "TauCetiQueue/recovery",
            },
        )
        with opener.open(request, timeout=10) as response:
            health = json.loads(response.read(65536))
        import uuid

        recovered = {"id": str(uuid.UUID(health["queue_source"])), "epoch": health["queue_fence"]["epoch"]}
        if type(recovered["epoch"]) is not int or not 0 < recovered["epoch"] < 2**53 - 2:
            parser.error("receiver returned an invalid source fence")
        args.state_file.parent.mkdir(parents=True, exist_ok=True)
        # A new recovery path is required; do not overwrite another process's
        # locked identity. Stop that sidecar explicitly before choosing recovery.
        fd = os.open(args.state_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as stream:
            json.dump(recovered, stream)
            stream.flush()
            os.fsync(stream.fileno())
    with ExitStack() as stack:
        identity = stack.enter_context(PublisherIdentity(args.state_file)) if args.state_file else None
        publisher = SnapshotPublisher(identity) if identity else None
        return run(args, stop, facts, opener, publisher)


def run(args, stop, facts, opener, publisher):
    task_observations = TaskObservations()
    retry_seconds = args.interval
    while not stop.is_set():
        iteration_started = time.monotonic()
        succeeded = False
        try:
            try:
                with urllib.request.urlopen(args.status_url, timeout=3) as response:
                    status = json.loads(response.read(MAX_BYTES))
            except (OSError, ValueError, http.client.HTTPException):
                status = {}
            observation = project(
                args.database,
                status=status,
                worker_roots=args.worker_root,
                facts=facts,
                task_observations=task_observations,
                public_repositories=args.public_repository or ("TauCetiProject/TauCeti",),
            )
            payload = encode(observation)
            if args.output:
                args.output.parent.mkdir(parents=True, exist_ok=True)
                temporary = args.output.with_suffix(".tmp")
                temporary.write_bytes(payload)
                os.replace(temporary, args.output)
            if args.publish_url:
                token = args.token_file.read_text().strip()

                def send(frame, token=token):
                    request = urllib.request.Request(
                        args.publish_url,
                        data=encode(frame),
                        method="POST",
                        headers={
                            "Authorization": "Bearer " + token,
                            "Content-Type": "application/json",
                            "User-Agent": "tauceti-queue-monitor/1",
                        },
                    )
                    try:
                        with opener.open(request, timeout=10) as response:
                            return json.loads(response.read(65536))
                    except urllib.error.HTTPError as error:
                        try:
                            result = json.loads(error.read(65536))
                        except (OSError, ValueError):
                            result = {}
                        if error.code == 409 and result.get("code") == "resync":
                            raise ResyncRequired("receiver requires full state") from None
                        codes = {
                            401: "authentication",
                            403: "scope_mismatch",
                            429: "rate_limit",
                            503: "receiver_unavailable",
                        }
                        raise PublicationRejected(
                            error.code, result.get("code") or codes.get(error.code, "invalid_snapshot")
                        ) from None

                publisher.publish(observation, send)
                retry_seconds = args.interval
            elif not args.output:
                print(payload.decode(), flush=True)
            succeeded = True
        except (
            OSError,
            ValueError,
            sqlite3.Error,
            http.client.HTTPException,
            KeyError,
            TypeError,
            AttributeError,
        ) as error:
            # Exception text may contain private paths or request URLs. It is never telemetry.
            detail = str(error) if isinstance(error, PublicationRejected) else type(error).__name__
            print(f"queue monitoring: {detail}" + ("; retrying" if args.follow else ""), file=sys.stderr)
            retry_seconds = max(
                args.interval,
                300
                if isinstance(error, PublicationRejected) and 400 <= error.status < 500 and error.status != 429
                else min(60, retry_seconds * 2),
            )
            if not args.follow:
                return 1
        # Successful upload time is part of the sampling interval. Slow work
        # may overrun freely; yield before another sample instead of catching up
        # in a hot loop. Failures retain their full post-request retry backoff.
        wait_seconds = max(1, args.interval - (time.monotonic() - iteration_started)) if succeeded else retry_seconds
        if not args.follow or stop.wait(wait_seconds):
            break
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

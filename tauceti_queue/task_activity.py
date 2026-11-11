"""Observe known repair subprocesses locally; publish only bounded phases/counters.

This is independent of the worker's checkpoints so long-running, already-started
attempts remain observable. No commands, module names, paths or lease tokens leave
TaskObservations' public projection.
"""

from __future__ import annotations

import argparse
import json
import os
import stat
import time
from pathlib import Path

SCHEMA = "tauceti.queue.task-activity/v1"
FILENAME = "activity.json"
PHASES = {
    "building",
    "freezing_cache_outputs",
    "auditing",
    "audit_import",
    "audit_duplicates",
    "audit_axioms",
    "environment_lint",
    "agent_running",
}


def processes(trees, *, proc=Path("/proc")):
    """Read command arguments only for known compiler/driver executables in our trees."""
    found = {}
    try:
        boot = int(
            next(line.split()[1] for line in (proc / "stat").read_text().splitlines() if line.startswith("btime "))
        )
        ticks = os.sysconf("SC_CLK_TCK")
    except (OSError, ValueError, StopIteration):
        return found
    for process in proc.iterdir():
        if not process.name.isdigit():
            continue
        try:
            cwd = Path(os.readlink(process / "cwd"))
            tree = next((t for t in trees if cwd == t or cwd.is_relative_to(t)), None)
            if tree is None or process.stat().st_uid != os.getuid():
                continue
            command = (process / "comm").read_text().strip()
            phase, priority = None, 0
            if command in ("audit-driver", "lint-env-driver"):
                phase, priority = ("auditing", 4) if command == "audit-driver" else ("environment_lint", 5)
            elif command == "lake":
                with (process / "cmdline").open("rb") as stream:
                    arguments = stream.read(8192).split(b"\0")
                if len(arguments) > 1 and arguments[1] == b"build":
                    phase, priority = (
                        ("freezing_cache_outputs", 6)
                        if b"--no-build" in arguments and b"--rehash" in arguments
                        else ("building", 3)
                    )
            elif command in ("claude", ".claude-wrapped", "codex", ".codex-wrapped"):
                phase, priority = "agent_running", 1
            if phase is None:
                continue
            raw = (process / "stat").read_text()
            fields = raw[raw.rindex(")") + 2 :].split()
            started = boot + int(fields[19]) / ticks
            previous = found.get(tree)
            if previous is None or priority > previous[0]:
                found[tree] = (priority, phase, started)
        except (OSError, ValueError, IndexError):
            continue
    return found


def sample(roots, *, proc=Path("/proc"), now=None, builds=None):
    from .monitoring import BuildObservations

    now = time.time() if now is None else now
    builds = builds or BuildObservations()
    candidates = {}
    for root in roots:
        root = Path(root)
        if root.is_symlink() or (root / "worktrees").is_symlink():
            continue
        for tree in (root / "worktrees").glob("task-*"):
            task_id = tree.name.removeprefix("task-")
            if not task_id.isdigit() or tree.is_symlink():
                continue
            directory = root / "tasks" / task_id
            try:
                progress = json.loads((directory / "progress.json").read_text())
                token = progress.get("claim_token")
                if str(progress.get("task_id")) != task_id or not isinstance(token, str):
                    continue
                candidates[tree] = (directory, progress)
            except (OSError, ValueError, TypeError):
                continue
    results = []
    for tree, (_, phase, started) in processes(candidates, proc=proc).items():
        directory, progress = candidates[tree]
        # Existing agents can predate a new claimed attempt; never retag one.
        if started + 1 < progress.get("updated_at", 0):
            continue
        if phase == "auditing":
            marker = tree / ".lake/tauceti-repair-audit/audit-phases.tsv"
            try:
                if any(parent.is_symlink() for parent in [marker.parent, marker.parent.parent]):
                    raise ValueError("unsafe audit marker")
                descriptor = os.open(marker, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
                with os.fdopen(descriptor, "rb") as stream:
                    if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                        raise ValueError("not a regular marker")
                    markers = stream.read(8192).decode().splitlines()
                for line in markers:
                    name, stamp = line.split("\t")
                    stamp = float(stamp)
                    if name in ("audit-import", "audit-duplicates", "audit-axioms") and started - 1 <= stamp <= now:
                        phase, started = name.replace("-", "_"), stamp
            except (OSError, ValueError):
                pass
        record = dict(
            schema=SCHEMA,
            task_id=progress["task_id"],
            claim_token=progress["claim_token"],
            state=phase,
            observed_at=now,
            phase_started_at=started,
        )
        if phase == "building":
            build = builds.observe(directory, "artifacts_ready", progress["claim_token"], now, started)
            if build:
                record["build"] = build
        temporary = directory / (FILENAME + "." + str(os.getpid()) + ".tmp")
        temporary.write_text(json.dumps(record) + "\n")
        temporary.replace(directory / FILENAME)
        results.append(record)
    return results


def public(directory, token, captured):
    """Only a fresh observation bound to this private claim can override a checkpoint."""
    from .monitoring import iso

    try:
        path = directory / FILENAME
        if path.is_symlink() or path.stat().st_size > 8192:
            return None
        value = json.loads(path.read_text())
        if (
            value.get("schema") != SCHEMA
            or str(value.get("task_id")) != directory.name
            or value.get("claim_token") != token
            or value.get("state") not in PHASES
            or not 0 <= captured - value["observed_at"] <= 45
            or not 0 < value["phase_started_at"] <= value["observed_at"]
        ):
            return None
        result = dict(state=value["state"], observed_at=iso(value["phase_started_at"]))
        if value.get("state") == "building" and isinstance(value.get("build"), dict):
            build = value["build"]
            if (
                type(build.get("completed_tasks")) is int
                and type(build.get("total_tasks")) is int
                and 0 <= build["completed_tasks"] <= build["total_tasks"] <= 999999999
            ):
                from datetime import datetime

                times = {}
                for key in ["sampled_at", "log_updated_at", "last_progress_at"]:
                    timestamp = build.get(key)
                    if timestamp is not None:
                        datetime.fromisoformat(timestamp)
                    times[key] = timestamp
                result["build"] = dict(
                    stage="preparation",
                    completed_tasks=build["completed_tasks"],
                    total_tasks=build["total_tasks"],
                    **times,
                )
        return result
    except (OSError, ValueError, TypeError, KeyError):
        return None


def main():
    from .monitoring import BuildObservations

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", action="append", required=True, type=Path)
    parser.add_argument("--follow", action="store_true")
    parser.add_argument("--until-complete", type=Path)
    args = parser.parse_args()
    builds = BuildObservations()
    while True:
        if args.until_complete:
            try:
                if json.loads(args.until_complete.read_text()).get("state") == "complete":
                    return
            except (OSError, ValueError):
                pass
        sample(args.root, builds=builds)
        if not args.follow:
            return
        time.sleep(2)


if __name__ == "__main__":
    main()

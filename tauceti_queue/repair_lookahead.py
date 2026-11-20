"""Resolve a rebase task's later conflicts in parallel while the worker's serial replay works on the first.

A ``rebase_conflict`` or ``pre_rebase`` task replays its chain onto ``onto`` one commit at a time
(``repair_task.resolve_conflicts``) and hands each conflicting commit to an agent on the clean prefix
before it. On a large batch after a big change to ``main`` most of that time is agents resolving
conflicts one after another. Most of those conflicts are between one member and ``main``, so they do
not depend on the prefix.

When the replay first conflicts, the worker starts a ``Lookahead``: up to ``parallel_agents - 1``
helpers, each resolving a later member's commit on ``onto`` alone, in a worktree of its own and a child
process (``python -m tauceti_queue.repair_lookahead run <job>``, as ``repair_parallel``'s group agents
run, since the agent path keeps per-process state). A helper runs ``repair_task.resolve_one``, the
serial path's own code for one conflict: the numproj pre-pass, then the agent with its attempts and
explanation follow-up. An acceptable resolution is recorded under the task's stored resolutions
(``repair_task.store_resolution``) keyed by ``onto``'s tree, so a rerun of the task also finds it.

The members given to helpers are those whose commit, cherry-picked onto ``onto``, conflicts in a path
that ``main`` changed since the chain's base (a conflict only with an earlier member's lines is the
serial replay's), that did not land on ``main`` and that do not change the dependencies (a helper
shares the task's packages and never fetches its own). They are taken in chain order.

At each conflict the serial replay asks for the member (``wait``): a member no helper has started is
the serial path's own; a running one is waited for. A stored lookahead resolution is merged onto the
real prefix (``repair.recommit_resolution`` with ``made_on``) and checked exactly as a fresh
resolution is (conflict markers, review size, no-repair members, absorbed later members, the rest of
the replay); one that does not merge or is not acceptable is resolved by the serial agent as before.
The submitted chain goes through the queue's checks and staging as any other.

Builds: every helper's agent build script takes the task worktree's build lock, the one the serial
agent's builds take, so the task runs one Lean build at a time however many agents are thinking. The
helpers run no other build; the numproj pre-pass only elaborates its own tool on the conflicting files.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import contextlib
import json
import os
import signal
import subprocess
import sys
import threading
from pathlib import Path

from tauceti_queue import chain as chain_tool
from tauceti_queue import repair, repair_audit, repair_parallel
from tauceti_queue.repair import git

DEPENDENCY_FILES = ("lean-toolchain", "lake-manifest.json")


def log(message: str) -> None:
    from tauceti_queue import repair_task

    repair_task.log(message)


class Lookahead:
    """The helpers of one rebase task; see the module docstring. ``after`` is the first conflicting
    commit, which the serial replay resolves itself."""

    def __init__(self, cfg, task, worktree: Path, logdir: Path, cancel: threading.Event, env_lake, artifacts, *, after):
        self.cfg, self.task, self.worktree, self.logdir = cfg, task, worktree, logdir
        self.env_lake, self.artifacts = env_lake, artifacts
        self.task_cancel = cancel
        self.stop = threading.Event()
        self.lock = threading.Lock()
        self.futures: dict[str, concurrent.futures.Future] = {}
        self.taken: set[str] = set()
        self.free: list[Path] = []
        self.made: list[Path] = []
        self.agent_records: list[dict] = []
        self.pool = concurrent.futures.ThreadPoolExecutor(
            max_workers=max(1, int(cfg.parallel_agents) - 1), thread_name_prefix="lookahead"
        )
        self.feeder = threading.Thread(target=self._feed, args=(after,), name="lookahead-feeder", daemon=True)
        self.feeder.start()

    def stopped(self) -> bool:
        return self.stop.is_set() or self.task_cancel.is_set()

    def candidates(self, after: str):
        """The later commits a helper resolves, in chain order (module docstring)."""
        clone, task = self.cfg.clone, self.task
        landed = chain_tool.landed_prs(clone, task.base, task.onto)
        changed = set(git(clone, "diff", "--name-only", task.base, task.onto).split("\n")) - {""}
        from tauceti_queue import repair_task

        for commit in chain_tool.chain_commits(clone, after, task.tip):
            if self.stopped():
                return
            parent = commit.parents[0]
            if commit.source_pr and chain_tool.landed_commit(landed, commit.source_pr):
                continue
            if commit.tree == chain_tool.tree_of(clone, parent):
                continue
            merged = chain_tool.merge_trees(clone, task.onto, commit.sha, merge_base=parent)
            if merged.clean or not changed.intersection(merged.conflict_paths):
                continue
            if git(clone, "diff", "--name-only", parent, commit.sha, "--", *DEPENDENCY_FILES).strip():
                continue
            if repair_task.stored_resolution(clone, task.task_id, commit.sha, task.onto) is not None:
                continue
            yield commit

    def _feed(self, after: str) -> None:
        try:
            for commit in self.candidates(after):
                with self.lock:
                    if self.stopped() or commit.sha in self.taken:
                        continue
                    self.futures[commit.sha] = self.pool.submit(self._resolve, commit.sha, commit.source_pr)
        except Exception as error:  # noqa: BLE001 - the serial replay goes on without more helpers
            log(f"task {self.task.task_id}: lookahead stopped choosing members: {type(error).__name__}: {error}")

    def wait(self, commit: str) -> None:
        """The serial replay reached ``commit``: no helper starts it from now on, and a running one is
        waited for (its resolution, if any, is then stored)."""
        with self.lock:
            self.taken.add(commit)
            future = self.futures.get(commit)
        if future is None or future.cancel():
            return
        log(f"task {self.task.task_id}: waiting for the lookahead resolution of {commit[:12]}")
        with contextlib.suppress(Exception):
            future.result()

    def _checkout(self) -> Path:
        with self.lock:
            if self.free:
                return self.free.pop()
            path = self.cfg.root / "worktrees" / f"task-{self.task.task_id}-lookahead-{len(self.made) + 1}"
            self.made.append(path)
        repair.create_worktree(self.worktree, self.task.onto, path)
        packages = self.worktree / ".lake" / "packages"
        if self.cfg.seed_packages:
            repair.link_shared_packages(path, self.cfg.seed_packages)
        elif packages.is_dir() or packages.is_symlink():
            repair.link_shared_packages(path, packages.resolve())
        if self.cfg.worker_audits:
            repair_audit.write_settings(
                path,
                base=self.task.validation_base,
                tools_root=self.cfg.root / repair_audit.TOOLS_DIR,
                lake=tuple(self.cfg.lake),
            )
        return path

    def _resolve(self, commit: str, source_pr: str) -> None:
        if self.stopped():
            return
        from tauceti_queue import repair_task

        eligible, _skipped = repair_task.select_providers(self.cfg)
        if not eligible:
            log(f"task {self.task.task_id}: no eligible provider for the lookahead of {commit[:12]}")
            return
        try:
            path = self._checkout()
        except Exception as error:  # noqa: BLE001 - the serial replay resolves it instead
            log(f"task {self.task.task_id}: no lookahead worktree for {commit[:12]}: {error}")
            return
        try:
            result = self._run_child(path, commit, source_pr, eligible)
        finally:
            with self.lock:
                self.free.append(path)
        with self.lock:
            self.agent_records.extend(result.get("agents") or [])
        if result.get("status") == "ok" and not self.stopped():
            repair_task.store_resolution(self.cfg.clone, self.task.task_id, commit, self.task.onto, result["tip"])
            log(f"task {self.task.task_id}: lookahead resolved {source_pr}'s commit {commit[:12]} on the target")
        else:
            log(
                f"task {self.task.task_id}: lookahead left {source_pr}: {result.get('status')} {result.get('reason') or ''}"
            )

    def _run_child(self, path: Path, commit: str, source_pr: str, providers: list[str]) -> dict:
        directory = self.cfg.tasks_dir / self.task.task_id / "lookahead" / commit[:12]
        logdir = self.logdir / f"lookahead-{commit[:12]}"
        directory.mkdir(parents=True, exist_ok=True)
        logdir.mkdir(parents=True, exist_ok=True)
        result_path = directory / "result.json"
        result_path.unlink(missing_ok=True)
        job = {
            "cfg": repair_parallel.config_to_json(self.cfg),
            "task": self.task.to_json(),
            "worktree": str(path),
            "commit": commit,
            "source_pr": source_pr,
            "logdir": str(logdir),
            "state_dir": str(directory),
            "result": str(result_path),
            "env_lake": self.env_lake,
            "artifacts": self.artifacts,
            "build_lock": str(repair_parallel.lock_path(self.worktree)),
            "providers": list(providers),
        }
        job_path = directory / "job.json"
        job_path.write_text(json.dumps(job, default=str))
        env = dict(os.environ)
        if self.cfg.api_key:
            env[repair_parallel.API_KEY_ENV] = self.cfg.api_key
        with (logdir / "runner.log").open("ab") as output:
            proc = subprocess.Popen(
                [sys.executable, "-m", "tauceti_queue.repair_lookahead", "run", str(job_path)],
                cwd=repair_parallel.REPO_ROOT,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=output,
                stderr=subprocess.STDOUT,
            )
            while proc.poll() is None:
                if self.stop.wait(0.2) or self.task_cancel.is_set():
                    proc.terminate()
                    try:
                        proc.wait(60)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait()
                    break
        if result_path.exists():
            return json.loads(result_path.read_text())
        return {"status": "failed", "reason": f"the lookahead runner exited {proc.returncode}"}

    def close(self) -> None:
        """Stop every helper (a running agent is terminated) and remove the worktrees."""
        self.stop.set()
        with self.lock:
            for future in self.futures.values():
                future.cancel()
        self.pool.shutdown(wait=True)
        self.feeder.join()
        if not self.cfg.keep_worktree:
            for path in self.made:
                with contextlib.suppress(Exception):
                    repair.remove_worktree(self.worktree, path)


def run_job(job_path: Path) -> int:
    """The child's entry: resolve the job's commit on the task's ``onto`` with ``repair_task.resolve_one``
    and write ``{"status", "tip", "reason", "agents"}`` to the job's result file. SIGTERM cancels."""
    from tauceti_queue import repair_task

    job = json.loads(job_path.read_text())
    cfg = repair_parallel.config_from_json(job["cfg"], repair_task.RunnerConfig)
    task = repair_task.Task.from_json(job["task"])
    repair_task.require_task_execution(cfg=cfg, task=task)
    cfg.api_key = os.environ.pop(repair_parallel.API_KEY_ENV, None) or None
    cancel = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: cancel.set())
    commit, source_pr, onto = job["commit"], job["source_pr"], task.onto
    worktree = Path(job["worktree"])
    agents: list[dict] = []

    def accept(tip: str, report: dict):
        if not report["ok"] or report["unexplained"]:
            return None
        problem = repair_task.no_repair_problem(cfg.clone, task, source_pr, onto, tip)
        if problem:
            report["problems"].append(problem)
            report["ok"] = False
            return None
        return True

    entry = {
        "kind": "conflict",
        "source_pr": source_pr,
        "commit": commit,
        "paths": list(chain_tool.merge_trees(cfg.clone, onto, commit, merge_base=f"{commit}^").conflict_paths),
        "onto": onto,
        "message": f"replaying the chain onto {onto[:12]} conflicts at {source_pr}",
        "pending": [],
        "clean_prefix_tip": onto,
        "landed_on_base": [],
    }
    try:
        result = repair_task.resolve_one(
            cfg,
            task,
            worktree,
            Path(job["logdir"]),
            cancel,
            repair_parallel.GroupState(Path(job["state_dir"])),
            job["env_lake"],
            prefix=onto,
            commit=commit,
            entry=entry,
            artifacts=job["artifacts"],
            agent_records=agents,
            accept=accept,
            label=f"lookahead-{commit[:12]}",
            build_lock=Path(job["build_lock"]),
            providers=job["providers"],
        )
        record = {"status": result["status"], "tip": result.get("tip"), "reason": result.get("reason")}
    except Exception as error:  # noqa: BLE001 - the serial replay resolves it instead
        record = {"status": "failed", "reason": f"{type(error).__name__}: {error}"}
    record["agents"] = agents
    result_path = Path(job["result"])
    tmp = result_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(record, default=str))
    os.replace(tmp, result_path)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m tauceti_queue.repair_lookahead", description=__doc__.split("\n\n")[0]
    )
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run", help="resolve one commit on the target from its job file (the worker's child)")
    run.add_argument("job", type=Path)
    args = parser.parse_args(argv)
    return run_job(args.job)


if __name__ == "__main__":
    sys.exit(main())

"""Durable local verification continuation, separate from publication/workspace recovery.

Only the ordinary supervisor consumes these jobs. It launches no model and performs no remote
reads. Completed receipts are left for the normal publication guards; this module never pushes.
"""

from __future__ import annotations

import contextlib
import fcntl
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

from .runtime_status import read_json, report_runtime
from .verification_scope import git


def snapshot(root, target, names):
    """Independent Git/source/dependency copy: never reset or replay into an agent's workspace."""
    index_tree = git(root, "write-tree").decode().strip()
    subprocess.run(
        ["git", "clone", "--local", "--no-hardlinks", "--dissociate", "--no-checkout", str(root), str(target)],
        check=True,
        capture_output=True,
    )
    git(target, "read-tree", index_tree)
    for name in names:
        source, dest = root / name, target / name
        if not source.exists() and not source.is_symlink():
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, dest, follow_symlinks=False)
    packages = root / ".lake/packages"
    if packages.exists():
        # Copy source installations, not potentially live build outputs or shared writable caches.
        shutil.copytree(
            packages, target / ".lake/packages", symlinks=False, ignore=shutil.ignore_patterns(".lake", "__pycache__")
        )


def prepare_job(verification, inputs):
    from .verification import atomic, digest

    if verification.job:
        job = verification.job
    else:
        job = dict(
            schema="tauceti-verification-job/v1",
            id=verification.id,
            priority=time.time_ns(),
            root=str(verification.root),
            base=verification.base,
            stage=verification.requested[0] if len(verification.requested) == 1 else "all",
            policy=verification.policy,
            reuse=verification.reuse,
            source_digest=digest(inputs["sources"]),
            lint_modules=verification.lint_modules and str(verification.lint_modules),
            receipt=str(verification.path),
            status="pending",
            attempts=0,
            next_at=0,
        )
        if verification.lint_modules:
            target = verification.directory / "explicit-lint-modules.txt"
            shutil.copy2(verification.lint_modules, target)
            job["lint_modules"] = str(target)
    job["receipt"] = str(verification.path)
    job["lease"] = str(verification.directory / "live.lock")
    atomic(verification.state / "verification-pending" / f"{job['id']}.json", job)
    return job


def preserve_candidate(verification, *, disposable_only=False):
    """Preserve before publishing a queued job; reused phases need no candidate copy."""
    from .verification import atomic, digest, inventory

    job = verification.job
    if job.get("isolated") or disposable_only and os.environ.get("TAUCETI_VERIFY_BUBBLE") != "1":
        return
    target = verification.state / "verification-candidates" / job["id"]
    target.parent.mkdir(parents=True, exist_ok=True)
    snapshot(verification.root, target, inventory(verification.root, verification.base))
    if digest(inventory(verification.root, verification.base)) != job["source_digest"]:
        raise ValueError("candidate changed while preserving verification job")
    if digest(inventory(target, verification.base)) != job["source_digest"]:
        raise ValueError("preserved candidate inventory differs")
    job.update(root=str(target), isolated=True)
    atomic(verification.state / "verification-pending" / f"{job['id']}.json", job)


def finish_job(verification):
    from .verification import atomic

    job = verification.job
    job["receipt"] = str(verification.path)
    status = verification.receipt["status"]
    path = verification.state / "verification-pending" / f"{job['id']}.json"
    if status in {"success", "failed"}:
        job["status"] = status
        atomic(verification.state / "verification-jobs" / f"{job['id']}.json", job)
        path.unlink(missing_ok=True)
    else:
        job.update(status="pending", next_at=time.time() + 5)
        atomic(path, job)
        verification.receipt.update(
            result_type="pending",
            continuation_job=str(path),
            next_action="supervisor: resume verification without a model",
        )


def resume_pending(cfg, *, seconds=7200):
    """Perform one bounded local continuation before model admission/survey. Return whether attempted."""
    from .verification import Verification, atomic, digest, inventory

    if getattr(cfg, "state", None) is None:
        return False
    waiting = False
    for state in (cfg.state, cfg.state / "verification-state"):
        pending = state / "verification-pending"
        jobs = [(path, read_json(path)) for path in pending.glob("*.json")]
        jobs.sort(key=lambda pair: pair[1].get("priority", 0))
        for path, job in jobs:
            if not job:
                continue
            identifier = job.get("id")
            if (
                job.get("schema") != "tauceti-verification-job/v1"
                or not isinstance(identifier, str)
                or not re.fullmatch(r"[0-9a-f]{32}", identifier)
                or path.name != f"{identifier}.json"
                or job.get("stage") not in {"all", "build", "axioms", "module-system", "lint", "shim-expiry"}
                or not isinstance(job.get("root"), str)
                or not Path(job["root"]).is_absolute()
                or not isinstance(job.get("base"), str)
                or not isinstance(job.get("source_digest"), str)
                or not isinstance(job.get("priority"), int)
                or not isinstance(job.get("attempts"), int)
                or not isinstance(job.get("next_at"), (int, float))
                or not isinstance(job.get("reuse"), bool)
                or job.get("policy") not in {"ordinary", "full-lint", "lint-repair", "bump"}
            ):
                report_runtime("verification-pending", detail=f"invalid verification job: {path}")
                continue
            if job.get("next_at", 0) > time.time():
                waiting = waiting or job.get("status") == "pending"
                continue
            # Bubble writes paths in its mounted namespace. Rebase only that known state
            # prefix; ordinary host paths are already absolute.
            for key in ("root", "lease", "receipt", "lint_modules"):
                value = job.get(key)
                if value and Path(value).is_relative_to("/opt/verification-state"):
                    job[key] = str(state / Path(value).relative_to("/opt/verification-state"))
            lock = state / "verification-jobs" / f"{job['id']}.lock"
            lock.parent.mkdir(parents=True, exist_ok=True)
            with lock.open("a+") as owner:
                try:
                    fcntl.flock(owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    continue
                # Respect both the worker's round and the original helper's live lease.
                with (cfg.state / "round.lock").open("a+") as round_lock:
                    try:
                        fcntl.flock(round_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError:
                        return False
                    lease_path = job.get("lease")
                    with contextlib.ExitStack() as stack:
                        if lease_path and Path(lease_path).exists():
                            lease = stack.enter_context(Path(lease_path).open("a+"))
                            try:
                                fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
                            except BlockingIOError:
                                continue
                        from .survey import Counters
                        from .verification import refund_capacity_wait

                        refund_capacity_wait(state, Counters(cfg), interrupted_round=True)
                        job["attempts"] += 1
                        root = Path(job["root"])
                        try:
                            if (
                                job.get("isolated")
                                and root.resolve() != (state / "verification-candidates" / identifier).resolve()
                            ):
                                raise ValueError("isolated candidate path does not match its job")
                            if digest(inventory(root, job["base"])) != job["source_digest"]:
                                raise ValueError("candidate source changed; preserved job requires reconciliation")
                            if not job.get("isolated"):
                                target = state / "verification-candidates" / job["id"]
                                target.parent.mkdir(parents=True, exist_ok=True)
                                if target.exists():
                                    shutil.rmtree(target)  # previous interrupted snapshot, never the source
                                snapshot(root, target, inventory(root, job["base"]))
                                if digest(inventory(target, job["base"])) != job["source_digest"]:
                                    raise ValueError("candidate changed during continuation snapshot")
                                root = target
                                job.update(root=str(root), isolated=True)
                            atomic(path, job)
                            report_runtime(
                                "verifying",
                                detail="mechanically continuing preserved candidate",
                                verification_job=job["id"],
                            )
                            # Supervisor slices have their own deadline, independent of the expired
                            # model round. Reserve no model checkpoint margin inside this slice.
                            old = os.environ.get("TAUCETI_VERIFY_CHECKPOINT_MARGIN")
                            os.environ["TAUCETI_VERIFY_CHECKPOINT_MARGIN"] = "0"
                            try:
                                result = Verification(
                                    root,
                                    state,
                                    base=job["base"],
                                    stage=job["stage"],
                                    reuse=job["reuse"],
                                    policy=job["policy"],
                                    lint_modules=job.get("lint_modules"),
                                    deadline=time.time() + seconds,
                                    job=job,
                                ).run()
                            finally:
                                if old is None:
                                    os.environ.pop("TAUCETI_VERIFY_CHECKPOINT_MARGIN", None)
                                else:
                                    os.environ["TAUCETI_VERIFY_CHECKPOINT_MARGIN"] = old
                            # Incomplete preparation may happen before prepare_job updates its lease.
                            job.update(
                                receipt=result["receipt"], lease=str(Path(result["receipt"]).parent / "live.lock")
                            )
                            if result["status"] == "incomplete":
                                job.update(next_at=time.time() + 5, status="pending")
                                atomic(path, job)
                        except (OSError, ValueError, subprocess.CalledProcessError) as error:
                            job.update(error=str(error), next_at=time.time() + 60, status="blocked")
                            atomic(path, job)
                            report_runtime("verification-pending", detail=str(error), verification_job=job["id"])
                        return True
    return waiting

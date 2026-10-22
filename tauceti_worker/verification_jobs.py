"""Durable local verification continuation, separate from publication/workspace recovery.

Only the ordinary supervisor consumes these jobs. It launches no model and performs no remote
reads. Completed receipts are left for the normal publication guards; this module never pushes.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from .paths import HERE
from .runtime_status import read_json, report_runtime
from .verification_scope import git
from .verification_storage import Pressure


def copy_file(source, target):
    """Use independent CoW extents when available, with a portable byte-copy fallback."""
    try:
        with open(source, "rb") as incoming, open(target, "wb") as outgoing:
            fcntl.ioctl(outgoing.fileno(), 0x40049409, incoming.fileno())  # Linux FICLONE
        shutil.copystat(source, target)
        return target
    except OSError:
        return shutil.copy2(source, target)


def backing_source(source, build, lower):
    """Merged enumeration owns whiteouts; open real backing files for reflink."""
    source = Path(source)
    if not source.is_relative_to(build):
        return source
    relative = source.relative_to(build)
    private = build.parent / "tauceti-overlay-upper" / relative
    return private if private.is_file() else lower / relative


def snapshot_command():
    """Private subprocess entry; preserve retryable storage failures across IPC."""
    payload = json.load(sys.stdin)
    for name in ("root", "target", "immutable_packages"):
        if payload.get(name) is not None:
            payload[name] = Path(payload[name])
    try:
        snapshot(**payload)
    except Pressure as error:
        print(json.dumps(dict(pressure=error.reason, error=str(error))))
        raise SystemExit(75) from error


def snapshot(root, target, names, *, immutable_packages=None, include_builds=True):
    """A failed copy is disposable until the caller durably publishes isolated ownership."""
    from .shared_mathlib import remove_tree
    from .verification import atomic, digest
    from .verification_storage import candidate, release

    state = target.parent.parent
    expected = candidate(state, target.name)
    if target.parent.resolve() != expected.parent or target.exists() or target.is_symlink():
        raise ValueError("snapshot needs a new owned candidate path")
    marker = state / "verification-copy-incomplete" / f"{target.name}.json"
    atomic(marker, dict(version=1, source=str(root), source_digest=digest(names)))
    try:
        # After the agent namespace exits, this host path is only the sparse
        # upper (possibly with whiteouts), not a usable Mathlib build. Materialize
        # its merged cache while holding the existing lower's ordinary reader lease.
        from . import shared_mathlib
        from .verification import Incomplete

        upper = root / ".lake/packages/mathlib/.lake/build"
        shared = read_json(upper.parent / "tauceti-shared-mathlib.json")
        if include_builds and shared and not upper.is_mount():
            key = shared.get("key", "")
            if not re.fullmatch("[0-9a-f]{64}", key):
                raise Incomplete("invalid shared Mathlib snapshot identity", reason="preservation")
            pool = Path(shared.get("pool") or shared_mathlib.root())
            with shared_mathlib.key_lock(pool, key, exclusive=False) as lease:
                lower = pool / "entries" / key / "build"
                if lower.is_dir() and shared.get("boot_id") == shared_mathlib.boot_id():
                    work = upper.with_name("tauceti-overlay-work")
                    work.mkdir(exist_ok=True)
                    payload = dict(
                        root=str(root),
                        target=str(target),
                        names=names,
                        immutable_packages=str(immutable_packages) if immutable_packages else None,
                        include_builds=include_builds,
                    )
                    env = dict(os.environ)
                    package = str(Path(__file__).resolve().parent.parent)
                    env["PYTHONPATH"] = package + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
                    result = subprocess.run(
                        shared_mathlib.overlay_command(lower, upper, work, upper, pool, lease_fd=lease.fileno())
                        + [sys.executable, "-m", "tauceti_worker.verification_jobs"],
                        input=json.dumps(payload),
                        env=env,
                        text=True,
                        capture_output=True,
                        pass_fds=(lease.fileno(),),
                    )
                    if result.returncode == 75:
                        failure = json.loads(result.stdout)
                        raise Pressure(failure["error"], reason=failure["pressure"])
                    if result.returncode:
                        raise Incomplete(
                            "merged Mathlib snapshot failed: " + result.stderr[-1000:], reason="preservation"
                        )
                    return
            # A collected lower or pre-reboot disposable upper is a cache
            # miss. Rehydrate normally, with the normal service config.
            from .agents import host_build_cache_context

            shared_mathlib.detach_upper(upper, upper.parent / "tauceti-shared-mathlib.json")
            try:
                with host_build_cache_context(root, dict(os.environ), state / "verification-cache-prepare"):
                    return snapshot(root, target, names, immutable_packages=immutable_packages, include_builds=True)
            except Pressure:
                raise
            except Exception as error:
                raise Pressure(f"Mathlib cache preparation pending: {error}", reason="cache") from error
        return _snapshot(root, target, names, immutable_packages=immutable_packages, include_builds=include_builds)
    except BaseException:
        # Nothing in this directory has become an authoritative workspace. Owner death leaves
        # the same durable construction marker for bounded ordinary collection.
        with contextlib.suppress(OSError):
            remove_tree(target)
        if not target.exists():
            with contextlib.suppress(OSError, Pressure):
                release(state, target.name)
                marker.unlink(missing_ok=True)
        raise


def commit_copy(state, identifier):
    (state / "verification-copy-incomplete" / f"{identifier}.json").unlink(missing_ok=True)


def _snapshot(root, target, names, *, immutable_packages=None, include_builds=True):
    """Copy exact sources/index and warm build caches without editing the agent's index."""
    from .verification import Incomplete
    from .verification_storage import CopyBudget, reserve, settle

    try:
        amount = reserve(target.parent.parent, target.name)
        budget = CopyBudget(amount, target)

        build = root / ".lake/packages/mathlib/.lake/build"
        shared = read_json(build.parent / "tauceti-shared-mathlib.json")
        lower = None
        if shared and build.is_mount():
            from .shared_mathlib import root as shared_root

            lower = Path(shared.get("pool") or shared_root()) / "entries" / shared["key"] / "build"

        def copy(source, dest):
            budget.charge(Path(source))
            actual = backing_source(source, build, lower) if lower is not None else source
            return copy_file(actual, dest)

        index = git(root, "rev-parse", "--git-path", "index").decode().strip()
        index = Path(index) if Path(index).is_absolute() else root / index
        with tempfile.TemporaryDirectory(prefix="verification-index-") as temporary:
            private_index = Path(temporary) / "index"
            shutil.copy2(index, private_index)
            index_tree = (
                subprocess.run(
                    ["git", "-C", str(root), "write-tree"],
                    env=dict(os.environ, GIT_INDEX_FILE=str(private_index)),
                    capture_output=True,
                    check=True,
                )
                .stdout.decode()
                .strip()
            )
        # Git's clone writes outside copytree's callback; charge the source object inventory
        # before launching it. Never rely on reflink/hardlink savings for admission.
        gitdir = Path(git(root, "rev-parse", "--git-common-dir").decode().strip())
        gitdir = gitdir if gitdir.is_absolute() else root / gitdir
        for directory, dirs, files in os.walk(gitdir, followlinks=False):
            dirs[:] = [name for name in dirs if not (Path(directory) / name).is_symlink()]
            for name in files:
                budget.charge(Path(directory) / name)
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
            if source.is_symlink() and (
                Path(os.readlink(source)).is_absolute() or not source.resolve().is_relative_to(root)
            ):
                raise Incomplete(
                    f"candidate source symlink needs explicit recovery ownership: {name}", reason="preservation"
                )
            dest.parent.mkdir(parents=True, exist_ok=True)
            budget.charge(source)
            shutil.copy2(source, dest, follow_symlinks=False)
        lake = root / ".lake"
        if lake.exists():
            # Existing oleans/build traces avoid a cold Mathlib rebuild. These caches have the
            # same trust level as ordinary local verification; audits still own proof correctness.
            def ignore(directory, entries):
                return [
                    name
                    for name in entries
                    if name
                    in {
                        "__pycache__",
                        "tauceti-scope",
                        "tauceti-overlay-work",
                        "tauceti-overlay-upper",
                        "tauceti-shared-mathlib.json",
                        "build-before-sharing",
                        "build-shared-delta",
                    }
                    or not include_builds
                    and name in {"build", "build-before-sharing"}
                    and Path(directory).name == ".lake"
                    or immutable_packages is not None
                    and Path(directory) == lake
                    and name == "packages"
                    or target.is_relative_to(Path(directory) / name)
                ]

            shutil.copytree(lake, target / ".lake", symlinks=False, copy_function=copy, ignore=ignore)
        if immutable_packages is not None:
            (target / ".lake").mkdir(exist_ok=True)
            (target / ".lake/packages").symlink_to(immutable_packages, target_is_directory=True)
        with contextlib.suppress(OSError, Pressure):
            settle(target.parent.parent, target.name, budget.bytes + 65536, compact=not include_builds)
    except (OSError, subprocess.CalledProcessError) as error:
        raise Incomplete(f"candidate preservation incomplete: {error}", reason="preservation") from error


def prepare_job(verification, inputs):
    from .verification import Incomplete, atomic, digest, file_record

    with contextlib.ExitStack() as stack:
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
            key = digest(
                dict(
                    root=job["root"],
                    base=job["base"],
                    source=job["source_digest"],
                    stage=job["stage"],
                    policy=job["policy"],
                    environment=verification.environment(),
                    lint_modules=file_record(Path(verification.lint_modules)) if verification.lint_modules else None,
                )
            )
            index = verification.state / "verification-job-index" / f"{key}.json"
            index.parent.mkdir(parents=True, exist_ok=True)
            lock = stack.enter_context(index.with_suffix(".lock").open("a+"))
            fcntl.flock(lock, fcntl.LOCK_EX)
            previous = read_json(index).get("id")
            if isinstance(previous, str) and re.fullmatch(r"[0-9a-f]{32}", previous):
                pending = verification.state / "verification-pending" / f"{previous}.json"
                if pending.is_file() and not (verification.state / "verification-jobs" / f"{previous}.json").is_file():
                    verification.receipt.update(
                        result_type="pending",
                        continuation_job=str(pending),
                        next_action="supervisor: resume existing exact-candidate job without a model",
                    )
                    raise Incomplete("exact candidate already has a durable verification job", reason="already-pending")
            atomic(index, dict(id=job["id"]))
            if verification.lint_modules:
                target = verification.directory / "explicit-lint-modules.txt"
                shutil.copy2(verification.lint_modules, target)
                job["lint_modules"] = str(target)
        job["receipt"] = str(verification.path)
        job["lease"] = str(verification.directory / "live.lock")
        atomic(verification.state / "verification-pending" / f"{job['id']}.json", job)
        return job


def preserve_candidate(verification, *, disposable_only=False):
    """Preserve an interrupted or disposable candidate; reused phases need no candidate copy."""
    from .verification import atomic, digest, inventory

    job = verification.job
    if job.get("isolated") or disposable_only and os.environ.get("TAUCETI_VERIFY_BUBBLE") != "1":
        return
    target = verification.state / "verification-candidates" / job["id"]
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.resolve() == verification.root or target.resolve() in verification.root.parents:
        raise ValueError("candidate preservation path overlaps its source")
    if target.exists():
        shutil.rmtree(target)  # a partial copy from an interrupted preservation
    packages = (verification.root / ".lake/packages").resolve()
    immutable_packages = None
    # External trusted host installations outlive the source workspace. Keep their validated
    # identity instead of copying payload into a writable directory and losing the warm fast path.
    if os.environ.get("TAUCETI_VERIFY_BUBBLE") != "1" and not packages.is_relative_to(verification.root):
        registration = os.environ.get("TAUCETI_VERIFY_IMMUTABLE_INSTALLATIONS")
        if registration and read_json(Path(registration)).get(str(packages)):
            verification.installations.inventory(packages)
            if packages in verification.installations.validated:
                immutable_packages = packages
    snapshot(
        verification.root,
        target,
        inventory(verification.root, verification.base),
        immutable_packages=immutable_packages,
    )
    if digest(inventory(verification.root, verification.base)) != job["source_digest"]:
        raise ValueError("candidate changed while preserving verification job")
    if digest(inventory(target, verification.base)) != job["source_digest"]:
        raise ValueError("preserved candidate inventory differs")
    job.update(root=str(target), isolated=True)
    atomic(verification.state / "verification-pending" / f"{job['id']}.json", job)
    commit_copy(verification.state, job["id"])


def tool_readiness(job):
    from .verification import LOCAL_CHECKERS, digest, stamps

    paths = [path for name in ("lake", "lean", "bash", "python3", "git", "tar") if (path := shutil.which(name))]
    paths += [str(p) for p in Path(__file__).parent.glob("*.py")]
    paths += [str(HERE / "scripts" / name) for name in ("tauceti-verify", *LOCAL_CHECKERS)]
    paths.append(str(Path(os.environ.get("ELAN_HOME", str(Path.home() / ".elan"))) / "toolchains"))
    if registration := os.environ.get("TAUCETI_VERIFY_IMMUTABLE_INSTALLATIONS"):
        paths.append(registration)
    if job.get("toolchain_wait_path"):
        prefix = Path(job["toolchain_wait_path"])
        paths += [str(prefix), str(prefix / "bin" / "lean")]
    return digest(
        dict(
            path=os.environ.get("PATH"),
            elan=os.environ.get("ELAN_HOME"),
            stamps=stamps(paths),
            storage={
                name: os.environ.get("TAUCETI_VERIFY_" + name)
                for name in (
                    "CANDIDATE_BYTES",
                    "SOURCE_CANDIDATE_BYTES",
                    "CHECKPOINT_BYTES",
                    "STORAGE_BYTES",
                    "CHECKPOINT_POOL_BYTES",
                    "MIN_FREE_BYTES",
                    "MIN_FREE_PERCENT",
                )
            },
        )
    )


def finish_job(verification):
    from .verification import Incomplete, atomic

    job = verification.job
    job["receipt"] = str(verification.path)
    status = verification.receipt["status"]
    path = verification.state / "verification-pending" / f"{job['id']}.json"
    waiting_tools = verification.receipt.get("incomplete_reason") in {"tool-missing", "toolchain"}
    storage_wait = verification.receipt.get("incomplete_reason") == "disk-pressure"
    resumable = (
        verification.receipt.get("incomplete_reason")
        in {"deadline", "signal", "disk-pressure", "storage-limit", "verification-busy"}
        or waiting_tools
    )
    if resumable and not job.get("isolated") and not storage_wait:
        try:
            preserve_candidate(verification)
        except (Incomplete, OSError, ValueError, Pressure) as error:
            # Preservation errors retain source/checkpoint artifacts and never become tool failures.
            if isinstance(error, Pressure):
                storage_wait = True
                verification.receipt.update(
                    error=str(error),
                    incomplete_reason="verification-busy" if error.reason == "busy" else "disk-pressure",
                )
            else:
                verification.receipt.update(error=str(error), incomplete_reason="preservation")
                resumable = False
    if storage_wait and not job.get("isolated"):
        # A live or container-only checkout is not an independent continuation workspace.
        # Permit a fresh exact-source invocation after reclamation rather than deduplicating
        # it against a pending job the supervisor cannot safely execute.
        resumable = False
    if status in {"success", "failed"} or not resumable:
        job["status"] = status
        job["error"] = verification.receipt.get("error")
        atomic(verification.state / "verification-jobs" / f"{job['id']}.json", job)
        path.unlink(missing_ok=True)
        if status == "incomplete":
            verification.receipt.update(
                result_type="incomplete",
                next_action=(
                    "reclaim verification storage and retry verify in this workspace"
                    if storage_wait
                    else "repair verification inputs and invoke verify again"
                ),
            )
    else:
        job.update(
            status="blocked-tools" if waiting_tools else "pending",
            next_at=time.time() + (60 if waiting_tools or storage_wait else 5),
        )
        if waiting_tools:
            job["tool_readiness"] = tool_readiness(job)
        atomic(path, job)
        verification.receipt.update(
            result_type="pending",
            continuation_job=str(path),
            next_action=(
                "supervisor: reclaim idle build caches, then resume verification without a model"
                if storage_wait
                else "supervisor: resume when local toolchain availability changes"
                if waiting_tools
                else "supervisor: resume verification without a model"
            ),
        )


_CHILDREN = {}


def _settle_child(state, path, job, process, result_path):
    from .verification import atomic

    process.wait()
    result = read_json(result_path)
    if process.returncode < 0 and path.exists():
        current = read_json(path) or job
        current.update(status="pending", next_at=time.time() + 5)
        atomic(path, current)
        return
    if path.exists() and (not result.get("receipt") or not result.get("continuation_job")):
        current = read_json(path) or job
        current.update(
            status="incomplete", error=result.get("error", "mechanical helper exited without a durable result")
        )
        atomic(state / "verification-jobs" / path.name, current)
        path.unlink(missing_ok=True)


def reap_continuations(cfg):
    key = str(getattr(cfg, "state", ""))
    active = []
    for state, path, job, process, result_path in _CHILDREN.get(key, []):
        if process.poll() is None:
            active.append((state, path, job, process, result_path))
        else:
            _settle_child(state, path, job, process, result_path)
    _CHILDREN[key] = active


def stop_continuations(cfg):
    for state, path, job, process, result_path in _CHILDREN.pop(str(getattr(cfg, "state", "")), []):
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        _settle_child(state, path, job, process, result_path)


def resume_pending(cfg, *, seconds=7200, background=False):
    """Perform one bounded local continuation before model admission/survey. Return whether attempted."""
    from .verification import Incomplete, atomic, digest, inventory

    if getattr(cfg, "state", None) is None:
        return False
    from .verification_storage import Pressure, housekeep, reserve, validate_settings

    housekeep((cfg.state, cfg.state / "verification-state"))
    try:
        validate_settings()
    except Pressure as error:
        report_runtime("verification-pending", detail=str(error))
        return False
    reap_continuations(cfg)
    if os.environ.get("TAUCETI_VERIFY_CONTINUATION", "1") != "1":
        return False
    if background and _CHILDREN.get(str(cfg.state)):
        return False
    jobs = [
        (state, path, read_json(path))
        for state in (cfg.state, cfg.state / "verification-state")
        for path in (state / "verification-pending").glob("*.json")
    ]
    jobs.sort(key=lambda row: row[2].get("priority") if isinstance(row[2].get("priority"), int) else 0)
    for state, path, job in jobs:
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
            or job.get("lease") is not None
            and not isinstance(job["lease"], str)
            or job.get("lint_modules") is not None
            and not isinstance(job["lint_modules"], str)
            or job.get("policy") not in {"ordinary", "full-lint", "lint-repair", "bump"}
        ):
            report_runtime("verification-pending", detail=f"invalid verification job: {path}")
            continue
        if (state / "verification-jobs" / path.name).is_file():
            path.unlink(missing_ok=True)  # terminal publication survived an interrupted unlink
            continue
        if job.get("next_at", 0) > time.time():
            continue
        if job.get("status") == "blocked-tools" and job.get("tool_readiness") == tool_readiness(job):
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
                if background:
                    return False
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
                    root = Path(job["root"])
                    try:
                        root = root.resolve()
                        candidates = state.resolve() / "verification-candidates"
                        target = candidates / identifier
                        # Resolve the runtime state alias, never a replaceable candidate leaf
                        # before rmtree. A planted symlink must not redirect cleanup or tools.
                        if candidates.is_symlink() or target.is_symlink():
                            raise ValueError("continuation snapshot path is not an owned candidate")
                        if job.get("isolated") and root != target:
                            raise ValueError("isolated candidate path does not match its job")
                        if digest(inventory(root, job["base"])) != job["source_digest"]:
                            raise ValueError("candidate source changed; preserved job requires reconciliation")
                        if not job.get("isolated"):
                            if target == root or target in root.parents or root in target.parents:
                                raise ValueError("continuation snapshot path overlaps its source")
                            target.parent.mkdir(parents=True, exist_ok=True)
                            if target.exists():
                                shutil.rmtree(target)  # previous interrupted snapshot, never the source
                            snapshot(root, target, inventory(root, job["base"]))
                            if digest(inventory(target, job["base"])) != job["source_digest"]:
                                raise ValueError("candidate changed during continuation snapshot")
                            root = target
                            job["isolated"] = True
                        job["root"] = str(root)
                        atomic(path, job)
                        commit_copy(state, identifier)
                        reserve(state, identifier)
                        job["attempts"] += 1
                        atomic(path, job)
                        report_runtime(
                            "verifying",
                            detail="mechanically continuing preserved candidate",
                            verification_job=job["id"],
                        )
                        # A fresh installed helper binds receipts to the code actually loaded
                        # after deployment. Its signal handlers cannot swallow supervisor stop.
                        env = dict(
                            os.environ,
                            TAUCETI_VERIFY_STATE=str(state),
                            TAUCETI_VERIFY_POLICY=job["policy"],
                            TAUCETI_VERIFY_CHECKPOINT_MARGIN="0",
                            TAUCETI_ROUND_DEADLINE=str(time.time() + seconds),
                            TAUCETI_VERIFY_JOB_FD=str(owner.fileno()),
                            TAUCETI_VERIFY_PYTHON=sys.executable,
                        )
                        argv = [
                            sys.executable,
                            str(HERE / "scripts/tauceti-verify"),
                            "--base",
                            job["base"],
                            "--stage",
                            job["stage"],
                            "--resume-job",
                            str(path.resolve()),
                            "--json",
                        ]
                        if not job["reuse"]:
                            argv.append("--no-reuse")
                        if job.get("lint_modules"):
                            argv += ["--lint-modules", job["lint_modules"]]
                        if background:
                            result_path = state / "verification-jobs" / f"{job['id']}-{job['attempts']}.result.json"
                            log_path = result_path.with_suffix(".log")
                            # Independent candidate and job lease permit inference to continue
                            # while this helper verifies its independent candidate.
                            env["TAUCETI_RUNTIME_STATUS"] = str(result_path.with_suffix(".runtime.json"))
                            with result_path.open("w") as output, log_path.open("w") as log:
                                process = subprocess.Popen(
                                    argv,
                                    cwd=root,
                                    env=env,
                                    stdout=output,
                                    stderr=log,
                                    start_new_session=True,
                                    pass_fds=(owner.fileno(),),
                                )
                            _CHILDREN.setdefault(str(cfg.state), []).append((state, path, job, process, result_path))
                            return True
                        process = subprocess.Popen(
                            argv,
                            cwd=root,
                            env=env,
                            stdout=subprocess.PIPE,
                            text=True,
                            start_new_session=True,
                            pass_fds=(owner.fileno(),),
                        )
                        try:
                            output, _ = process.communicate()
                        except BaseException:
                            process.terminate()
                            try:
                                process.communicate(timeout=15)
                            except subprocess.TimeoutExpired:
                                process.kill()
                                process.communicate()
                            raise
                        import json

                        result = json.loads(output)
                        if result.get("status") not in {"success", "failed", "incomplete"}:
                            raise ValueError("mechanical helper did not return a typed verification result")
                        if result["status"] == "incomplete":
                            current = read_json(path) or job
                            current.update(receipt=result.get("receipt", current["receipt"]))
                            if result.get("receipt"):
                                current["lease"] = str(Path(result["receipt"]).parent / "live.lock")
                            if not result.get("continuation_job"):
                                current.update(
                                    status="incomplete", error=result.get("error", "helper preparation failed")
                                )
                                atomic(state / "verification-jobs" / path.name, current)
                                path.unlink(missing_ok=True)
                            else:
                                atomic(path, current)
                    except Pressure as error:
                        job.update(
                            error=str(error),
                            status="pending",
                            next_at=time.time() + (5 if error.reason == "busy" else 60),
                        )
                        atomic(path, job)
                        report_runtime("verification-pending", detail=str(error), verification_job=job["id"])
                        return False
                    except (Incomplete, OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as error:
                        job.update(error=str(error), status="incomplete")
                        atomic(state / "verification-jobs" / path.name, job)
                        path.unlink(missing_ok=True)
                        report_runtime("verification-incomplete", detail=str(error), verification_job=job["id"])
                    return True
    return False


if __name__ == "__main__":
    snapshot_command()

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
import signal
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from .paths import HERE
from .runtime_status import read_json, report_runtime
from .verification_scope import git
from .verification_storage import Pressure

# Everything under `.lake` is hard-linked from the source workspace, never copied, except small
# files that Lake or Git rewrite in place, where a link would let the live workspace change the
# candidate: traces, hashes, manifests and logs, and Git metadata outside its object store.
COPIED = (".trace", ".hash", ".json", ".toml", ".log", ".lock")
LINKS = "tauceti-links.json"
FORWARDED = (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)
CONTINUATION = "tauceti-continuation.json"
# Imports this package by path, so the verifier's environment (PYTHONPATH included) is unchanged.
CONTINUE = (
    "import sys; sys.path.insert(0, sys.argv[1]); "
    "from tauceti_worker.verification_jobs import continue_command; "
    "raise SystemExit(continue_command(sys.argv[2:]))"
)
SCRATCH = {
    CONTINUATION,
    LINKS,
    "__pycache__",
    "tauceti-scope",
    "tauceti-overlay-work",
    "tauceti-overlay-upper",
    "tauceti-shared-mathlib.json",
    "build-before-sharing",
    "build-shared-delta",
}


def snapshot(root, target, names, *, immutable_packages=None, include_builds=True):
    """A failed snapshot is disposable until the caller durably publishes isolated ownership.

    The candidate never holds a copy of Mathlib's build: a shared Mathlib build stays in the
    host's shared view, which the continuation attaches again (`continue_command`).
    """
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


def link(source, dest):
    """Hard-link one file; a failure stops the snapshot instead of falling back to a copy."""
    from .verification import Incomplete

    try:
        os.link(source, dest, follow_symlinks=True)
    except OSError as error:
        raise Incomplete(
            f"candidate preservation needs hard links ({error}); nothing was copied", reason="preservation"
        ) from error


def _shared_builds(lake):
    """Package build directories that are views of the host's shared Mathlib."""
    packages = lake / "packages"
    if not packages.is_dir():  # a symlinked packages tree is followed, as `_link_tree` does
        return set()
    return {
        package / ".lake/build"
        for package in packages.iterdir()
        if (package / ".lake/tauceti-shared-mathlib.json").exists() or (package / ".lake/build").is_mount()
    }


def _copied(relative):
    parts = relative.parts
    if ".git" in parts:
        return "objects" not in parts[parts.index(".git") :]
    return relative.name.endswith(COPIED)


def _link_tree(lake, dest, budget, *, include_builds, immutable_packages, target):
    """Rebuild `.lake` under `dest`: everything by hard link except small mutable metadata.

    Returns `{relative path: [inode, size, mtime_ns]}` of the links, so a continuation can
    prove that nothing wrote through a shared inode in the meantime.
    """
    shared = _shared_builds(lake)
    linked = {}
    for directory, dirs, files in os.walk(lake, followlinks=True):
        here = Path(directory)
        out = dest / here.relative_to(lake)
        out.mkdir(parents=True, exist_ok=True)
        out.chmod(out.stat().st_mode | 0o700)
        kept = []
        for name in dirs:
            path = here / name
            if (
                name in SCRATCH
                or name == "build-shared-deltas"
                and here.name == ".lake"
                or path in shared
                or target.is_relative_to(path)
                or not include_builds
                and name in {"build", "build-before-sharing"}
                and here.name == ".lake"
                or immutable_packages is not None
                and here == lake
                and name == "packages"
            ):
                continue
            kept.append(name)
        dirs[:] = kept
        for name in files:
            if name in SCRATCH or name.endswith(".setup.json"):
                continue  # Lake rewrites setup files before each compile; nothing reads them back.
            source = here / name
            info = source.stat()
            if not stat.S_ISREG(info.st_mode):
                continue
            relative = source.relative_to(lake)
            if _copied(relative):
                budget.charge(source)
                shutil.copy2(source, out / name)
            else:
                link(source, out / name)
                linked[str(relative)] = [info.st_ino, info.st_size, info.st_mtime_ns]
                budget.linked += info.st_size
    return linked


def links_intact(root):
    """Whether every hard link a snapshot made still has the inode, size and mtime it had.

    Lake replaces outputs instead of writing them in place, and Git checks files out by
    rename, but an editor or tool can write a linked source in place. Such a candidate is
    abandoned rather than verified against changed dependencies.
    """
    recorded = read_json(root / ".lake" / LINKS).get("links") or {}
    for relative, expected in recorded.items():
        try:
            info = (root / ".lake" / relative).lstat()
        except OSError:
            return False
        if [info.st_ino, info.st_size, info.st_mtime_ns] != expected:
            return False
    return True


def _snapshot(root, target, names, *, immutable_packages=None, include_builds=True):
    """Exact sources and index; Git objects and build payloads are hard links, never copies."""
    from .verification import Incomplete
    from .verification_storage import CopyBudget, reserve, settle

    try:
        amount = reserve(target.parent.parent, target.name)
        budget = CopyBudget(amount, target)
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
        # An explicit --local clone hard-links the object store and dies if a link fails, so the
        # candidate's refs and index are its own while its objects share the source's inodes.
        subprocess.run(
            ["git", "clone", "--local", "--no-checkout", "--quiet", str(root), str(target)],
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
        budget.linked = 0
        if lake.exists():
            linked = _link_tree(
                lake,
                target / ".lake",
                budget,
                include_builds=include_builds,
                immutable_packages=immutable_packages,
                target=target,
            )
            (target / ".lake" / LINKS).write_text(json.dumps(dict(version=1, links=linked)))
        if lake.exists() and _shared_builds(lake) and include_builds:
            # The continuation must attach the host's shared Mathlib view to build here.
            (target / ".lake").mkdir(exist_ok=True)
            (target / ".lake" / CONTINUATION).write_text(json.dumps(dict(shared_view=True)))
        if immutable_packages is not None:
            (target / ".lake").mkdir(exist_ok=True)
            (target / ".lake/packages").symlink_to(immutable_packages, target_is_directory=True)
        with contextlib.suppress(OSError, Pressure):
            # Count linked bytes too: once the source workspace drops its links, the
            # candidate is their only holder.
            settle(target.parent.parent, target.name, budget.bytes + budget.linked + 65536, compact=not include_builds)
    except subprocess.CalledProcessError as error:
        detail = (error.stderr or b"").decode(errors="replace").strip()[-300:]
        raise Incomplete(f"candidate preservation incomplete: {error} {detail}", reason="preservation") from error
    except OSError as error:
        raise Incomplete(f"candidate preservation incomplete: {error}", reason="preservation") from error


def _job_fds(env):
    return (int(env["TAUCETI_VERIFY_JOB_FD"]),) if env.get("TAUCETI_VERIFY_JOB_FD") else ()


def _rebaseline(verification):
    """Accept our own links: a hard link changes only the source inode's ctime and link count.

    Any change to content, size, mtime, inode or mode still fails the final watch.
    """
    from .verification import stamps

    before = getattr(verification, "static_stamps", None)
    if not before:
        return
    now = stamps(before)
    for path, old in before.items():
        new = now.get(path)
        if old and new and [*old[:4], *old[5:7]] == [*new[:4], *new[5:7]]:
            before[path] = new


def continue_command(argv):
    """Run the verifier inside the host's shared build view, as the agent's round did.

    The candidate holds no Mathlib build of its own, so the continuation attaches the shared
    Mathlib view and the TauCeti cache before verifying, and relinks its outputs afterwards.
    Exit 75 means the view or cache is unavailable now: the job stays pending.
    """
    from .agents import BuildCacheUnavailable, host_build_cache_context, relink_build_outputs
    from .shared_mathlib import lease_fds

    root = Path.cwd()
    env = dict(os.environ)
    if not read_json(root / ".lake" / CONTINUATION).get("shared_view"):
        # The candidate holds everything it builds against: become the verifier, unchanged.
        os.execve(argv[0], argv, env)
    logdir = Path(env["TAUCETI_VERIFY_STATE"]) / "verification-cache-prepare"
    logdir.mkdir(parents=True, exist_ok=True)
    keep = _job_fds(env)
    # Only the verifier's JSON receipt may reach stdout; preparation chatter goes to stderr.
    receipt = os.dup(1)
    os.dup2(2, 1)
    try:
        with host_build_cache_context(root, env, logdir) as prefix:
            if not prefix:
                # Without the shared view, Lake would rebuild Mathlib privately here.
                print("verification continuation: no shared Mathlib view; not building privately", file=sys.stderr)
                return 75
            # The supervisor stops a continuation by signalling this wrapper: pass it on, so
            # the verifier and its tools stop with it instead of outliving their owner.
            pending, children = [], []

            def relay(number, _frame):
                pending.append(number)
                for child in children:
                    child.send_signal(number)

            forward = {sig: signal.signal(sig, relay) for sig in FORWARDED}
            try:
                children.append(
                    subprocess.Popen(
                        [*(prefix or []), *argv], env=env, stdout=receipt, pass_fds=(*keep, *lease_fds(env))
                    )
                )
                for number in pending:
                    children[0].send_signal(number)
                returncode = children[0].wait()
            finally:
                for sig, previous in forward.items():
                    signal.signal(sig, previous)
            if returncode < 0 or pending:
                # Die the way the verifier did, so the supervisor sees a stopped continuation.
                number = -returncode if returncode < 0 else pending[0]
                signal.signal(number, signal.SIG_DFL)
                os.kill(os.getpid(), number)
            relink_build_outputs(root, env, phase="verify")
    except BuildCacheUnavailable as error:
        print(f"verification continuation: shared build view unavailable: {error}", file=sys.stderr)
        return 75
    return returncode


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
    _rebaseline(verification)
    if digest(inventory(verification.root, verification.base)) != job["source_digest"]:
        raise ValueError("candidate changed while preserving verification job")
    if digest(inventory(target, verification.base)) != job["source_digest"]:
        raise ValueError("preserved candidate inventory differs")
    job.update(root=str(target), isolated=True)
    atomic(verification.state / "verification-pending" / f"{job['id']}.json", job)
    commit_copy(verification.state, job["id"])


def tool_readiness(job):
    from . import verification_tools
    from .verification import LOCAL_CHECKERS, digest, stamps

    paths = [path for name in ("lake", "lean", "bash", "python3", "git", "tar") if (path := shutil.which(name))]
    paths += [str(p) for p in Path(__file__).parent.glob("*.py")]
    paths += [str(HERE / "scripts" / name) for name in ("tauceti-verify", *LOCAL_CHECKERS)]
    paths += [sys.executable, *verification_tools.files()]
    if config := verification_tools.configuration():
        paths.append(config)
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
    if (process.returncode < 0 or process.returncode == 75) and path.exists():
        current = read_json(path) or job
        current.update(status="pending", next_at=time.time() + (60 if process.returncode == 75 else 5))
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
                        if job.get("isolated") and not links_intact(root):
                            raise ValueError("a file linked into the candidate was written in place; abandoned")
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
                        # The candidate has no Mathlib build of its own: the wrapper attaches
                        # the host's shared view, then runs the installed helper inside it.
                        package = str(Path(__file__).resolve().parent.parent)
                        argv = [
                            sys.executable,
                            "-c",
                            CONTINUE,
                            package,
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
                        if process.returncode == 75:
                            raise Pressure("shared build view unavailable for the continuation", reason="cache")
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

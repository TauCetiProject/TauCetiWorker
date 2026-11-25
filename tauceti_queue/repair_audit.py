"""Worker-owned whole-library verification using main's pinned CI tools and policy files.

Every repair, preparation and rebase verification runs the check set staging runs: the final
build, then the compiled audit driver (import, duplicates, axioms), the module-system check, the
environment lint, the style lint and the dot-notation lint, all at once. The environment lint
first covers only the modules changed since the base, as an ordinary PR's preflight does; when
that round finds nothing in any check, the whole-library lint runs before the checks pass, so a
pass always includes it. Every finding of one round returns to the existing repair loop
together; staging remains the authoritative landing verdict.

Check phases and sampled process-tree RSS are measured in existing task logs. The compiled checks
read the build outputs through read-only mounts while they run together under the caller's build
lock. Every check is joined before cleanup.
"""

from __future__ import annotations

import concurrent.futures
import contextlib
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import tomllib
from pathlib import Path

from tauceti_queue import diagnostics, repair_sandbox, repair_tools

WORK_DIR = Path(".lake") / "tauceti-repair-audit"
# Under the TauCetiTools checkouts' root: the trusted pin and policy files read for each check.
POLICY_DIR = "policy"
# Under the same root, outside every candidate-writable directory (decision 200): each check's own
# materialized scripts and scope files, and the compiled drivers, one directory per driver key. The
# checks mount both read-only, so code the candidate runs can change neither the verifier nor its
# inputs.
VERIFIER_DIR = "verifier"
# In a pristine driver root: which seed packages its drivers were built against (part of the driver key).
SEED_FILE = "seed-packages"
DRIVERS_DIR = "drivers"
SETTINGS_FILE = "audit.json"  # in .tauceti-repair: what the agent's build script needs to run the checks
ERRORS_FILE = "errors.jsonl"  # repair_stream's error list, in .tauceti-repair
TOOLS_DIR = "tauceti-tools"  # under the worker's root: one TauCetiTools checkout per commit
URL_VARIABLE = "TAUCETI_QUEUE_TOOLS_URL"  # a ``{repo}`` template; tests point it at a local repository
DEFAULT_URL = "https://github.com/{repo}.git"
ENABLE_VARIABLE = "TAUCETI_QUEUE_WORKER_AUDITS"
AUDIT_TIMEOUT_SECONDS = 3000  # sandbox-build.sh's liveness budget for the audit driver
# The most changed modules the first environment lint covers before the whole-library lint takes
# its place. In changed-only mode ``lint-env.sh`` writes every selected module into one
# ``#[`A, `B, ...]`` array literal in its docstring-scan driver, and Lean's code generator stops
# with "maximum recursion depth reached" a little under 3,000 elements (v4.37.0-rc1, real
# TauCeti module names). Half that keeps a margin for longer names and toolchain changes.
CHANGED_LINT_MODULE_LIMIT = 1500
DRIVER_FAILURE = "LINT-ENV: FAIL \u2014 driver failure"  # lint-env.sh's fail() line for its own drivers
# Driver processes for the whole-library lint (lint-env.sh's LINT_SHARDS; scoped lints ignore it).
# Each imports the whole library and lints every second declaration. On chungus2 two shards took
# the linters phase from 514 s to 234 s, and four to 177 s. Up to three repair workers share
# tauchonki2's 96 cores with a staging guest, so two shards of 16 threads keep three concurrent
# full lints within the host. Only inside the sandbox, though: lint-env.sh starts each shard in its
# own process group, and without bwrap's --unshare-pid --die-with-parent nothing would collect the
# shards when ``_sandboxed`` kills its own group on a timeout.
LINT_SHARDS = "2"
MODULE_PATH = re.compile(r"^TauCeti(/[A-Za-z_][A-Za-z0-9_']*)+\.lean$")
REPO = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
SHA = re.compile(r"^[0-9a-f]{40}$")
TAIL_LINES = 30
TAIL_CHARS = 2000


class Unavailable(RuntimeError):
    """A check could not run at all (as opposed to running and reporting a finding)."""


def enabled(environ: dict | None = None) -> bool:
    value = (os.environ if environ is None else environ).get(ENABLE_VARIABLE, "1").strip().lower()
    return value not in ("0", "false", "no", "off")


# ----------------------------------------------------------------------------------------------
# Settings for the agent's build script


def write_settings(
    worktree: Path,
    *,
    base: str,
    tools_root: Path,
    lake: tuple[str, ...] = ("lake",),
    logdir: Path | None = None,
    policy_commit: str | None = None,
) -> Path:
    """``.tauceti-repair/audit.json``: the base the lint scope is taken against, where the TauCetiTools
    checkouts live and the Lake command, for ``.tauceti-repair/build``; and the ``policy_commit``
    (the queue's canonical commit when the task was claimed) whose ``tauceti.toml`` and ``scripts/``
    the checks take their TauCetiTools pin and policy files from, as staging takes them from ``main``."""
    path = worktree / repair_tools.TOOLS_DIR / SETTINGS_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    settings = {"base": base, "tools_root": str(tools_root), "lake": list(lake)}
    if logdir is not None:
        settings["logdir"] = str(logdir)
    if policy_commit and SHA.match(policy_commit):
        settings["policy_commit"] = policy_commit
    path.write_text(json.dumps(settings))
    return path


def read_settings(worktree: Path) -> dict | None:
    try:
        data = json.loads((worktree / repair_tools.TOOLS_DIR / SETTINGS_FILE).read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not data.get("base") or not data.get("tools_root"):
        return None
    return data


# ----------------------------------------------------------------------------------------------
# The pinned TauCetiTools scripts


def tools_pin(worktree: Path) -> tuple[str, str]:
    """``(repo, ref)`` of ``[tools.tools]`` in the worktree's ``tauceti.toml``."""
    path = worktree / "tauceti.toml"
    if not path.is_file():
        raise Unavailable("the worktree has no tauceti.toml naming the TauCetiTools pin")
    try:
        return parse_tools_pin(path.read_text())
    except OSError as error:
        raise Unavailable(f"tauceti.toml: {error}") from error


def parse_tools_pin(text: str) -> tuple[str, str]:
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as error:
        raise Unavailable(f"tauceti.toml: {error}") from error
    section = data.get("tools")
    tools = section.get("tools") if isinstance(section, dict) else None
    if not isinstance(tools, dict):
        raise Unavailable("tauceti.toml: missing [tools.tools]")
    repo, ref = str(tools.get("repo") or ""), str(tools.get("ref") or "")
    if not REPO.match(repo) or not (ref == "main" or SHA.match(ref)):
        raise Unavailable(
            f"tauceti.toml: [tools.tools] needs repo and ref main or a 40-hex SHA, found {repo!r} {ref!r}"
        )
    return repo, ref


def _run(argv: list, **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run([str(a) for a in argv], text=True, capture_output=True, check=False, **kwargs)


def _tail(text: str) -> str:
    return "\n".join(text.strip().splitlines()[-TAIL_LINES:])[-TAIL_CHARS:]


def tools_checkout(tools_root: Path, repo: str, ref: str) -> Path:
    """``tools_root/<sha>``, a checkout of TauCetiTools at ``ref``, fetched on first use.

    ``main`` is resolved to its current commit on every call, so its name is the commit audited."""
    url = os.environ.get(URL_VARIABLE, DEFAULT_URL).format(repo=repo)
    if not SHA.match(ref):
        try:
            proc = _run(["git", "ls-remote", url, f"refs/heads/{ref}"], timeout=60)
        except subprocess.TimeoutExpired as error:
            raise Unavailable(f"resolving {repo}@{ref}: {error}") from error
        resolved = proc.stdout.split()[:1] if proc.returncode == 0 else []
        if not resolved or not SHA.match(resolved[0]):
            raise Unavailable(f"resolving {repo}@{ref}: {_tail(proc.stderr or proc.stdout) or 'no such branch'}")
        ref = resolved[0]
    dest = tools_root / ref
    if (dest / "tauceti_tools").is_dir():
        return dest
    tools_root.mkdir(parents=True, exist_ok=True)
    scratch = Path(tempfile.mkdtemp(prefix=f".{ref[:12]}-", dir=tools_root))
    try:
        for argv in (
            ["git", "init", "--quiet", scratch],
            ["git", "-C", scratch, "fetch", "--quiet", "--depth", "1", url, ref],
            ["git", "-C", scratch, "checkout", "--quiet", "--detach", ref],
        ):
            proc = _run(argv, timeout=600)
            if proc.returncode != 0:
                raise Unavailable(f"fetching {repo}@{ref[:12]}: {_tail(proc.stderr or proc.stdout)}")
        try:
            os.rename(scratch, dest)
        except OSError:
            if not (dest / "tauceti_tools").is_dir():  # another worker's fetch did not win the rename either
                raise
    except subprocess.TimeoutExpired as error:
        raise Unavailable(f"fetching {repo}@{ref[:12]}: {error}") from error
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    return dest


def policy_tree(worktree: Path, commit: str, tools_root: Path) -> Path:
    """A fresh directory under ``tools_root/policy`` holding ``tauceti.toml`` and ``scripts/`` exactly as
    ``commit`` has them, read from the worktree's object store (fetched from ``origin`` when missing),
    never from the worktree's files: the trusted TauCetiTools pin and policy files a verification uses
    (decision 200). It is made for each check at an unpredictable path outside every sandbox bind, so no
    earlier candidate code can have planted anything in it or aimed a link at it; the caller removes it."""

    def run(argv: list) -> subprocess.CompletedProcess:
        try:
            return _run(["git", "-C", worktree, *argv], timeout=300)
        except subprocess.TimeoutExpired as error:
            raise Unavailable(f"reading the policy commit {commit[:12]}: {error}") from error

    if not SHA.match(commit):
        raise Unavailable(f"policy commit {commit!r} is not a commit SHA")
    if run(["cat-file", "-e", f"{commit}^{{commit}}"]).returncode != 0:
        run(["fetch", "--quiet", "origin", commit])
        if run(["cat-file", "-e", f"{commit}^{{commit}}"]).returncode != 0:
            raise Unavailable(f"the policy commit {commit[:12]} is not in the clone")
    parent = tools_root / POLICY_DIR
    parent.mkdir(parents=True, exist_ok=True)
    dest = Path(tempfile.mkdtemp(prefix=f"{commit[:12]}-", dir=parent))
    try:
        shown = run(["show", f"{commit}:tauceti.toml"])
        if shown.returncode != 0:
            raise Unavailable(f"the policy commit {commit[:12]} has no tauceti.toml")
        (dest / "tauceti.toml").write_text(shown.stdout)
        archive = dest / "scripts.tar"
        listed = run(["archive", "--format=tar", "-o", str(archive), commit, "scripts"])
        if listed.returncode != 0:
            raise Unavailable(f"the policy commit {commit[:12]} has no scripts/: {_tail(listed.stderr)}")
        try:
            unpacked = _run(["tar", "-xf", archive, "-C", dest], timeout=300)
        except subprocess.TimeoutExpired as error:
            raise Unavailable(f"unpacking the policy commit's scripts/: {error}") from error
        archive.unlink(missing_ok=True)
        if unpacked.returncode != 0 or not (dest / "scripts").is_dir():
            raise Unavailable(f"unpacking the policy commit's scripts/: {_tail(unpacked.stderr)}")
    except BaseException:
        shutil.rmtree(dest, ignore_errors=True)
        raise
    return dest


def materialize(checkout: Path, worktree: Path, dest: Path) -> Path:
    """The TauCetiTools setup action's ``materialize`` of the shared scripts, with the worktree's
    ``scripts/`` as the policy directory (baselines, allowlists)."""
    shutil.rmtree(dest, ignore_errors=True)
    dest.parent.mkdir(parents=True, exist_ok=True)
    argv = [sys.executable, "-m", "tauceti_tools", "materialize", dest]
    argv += ["--conf", worktree / "tauceti.toml", "--policy-dir", worktree / "scripts"]
    env = {**os.environ, "PYTHONPATH": str(checkout)}
    try:
        proc = _run(argv, cwd=checkout, env=env, timeout=300)
    except subprocess.TimeoutExpired as error:
        raise Unavailable(f"materializing the TauCetiTools scripts: {error}") from error
    if proc.returncode != 0 or not dest.is_dir():
        raise Unavailable(f"materializing the TauCetiTools scripts: {_tail(proc.stderr or proc.stdout)}")
    return dest


def _sandboxed(
    argv: list,
    worktree: Path,
    env: dict,
    extra: dict | None,
    log: Path,
    timeout: float | None,
    *,
    read_only_build: bool = False,
    ro_binds: tuple[Path, ...] = (),
    rw_binds: tuple[Path, ...] = (),
) -> dict:
    """``argv`` in the build sandbox with ``extra`` set inside it; output to ``log``. ``ro_binds`` and
    ``rw_binds`` are host directories outside the worktree made visible inside it, read-only or writable,
    at the same paths (the verifier's own directories)."""
    extra = {k: str(v) for k, v in (extra or {}).items()}
    run_env = {**env, **extra}
    wrapped = repair_sandbox.sandboxed_lake([str(a) for a in argv], worktree, run_env, extra_env=extra)
    if (ro_binds or rw_binds) and "--remount-ro" in wrapped:
        # Before the root becomes read-only: bwrap creates the mount points on it.
        at = wrapped.index("--remount-ro")
        binds = [
            arg for path in ro_binds for arg in ("--ro-bind", str(Path(path).resolve()), str(Path(path).resolve()))
        ]
        binds += [arg for path in rw_binds for arg in ("--bind", str(Path(path).resolve()), str(Path(path).resolve()))]
        wrapped[at:at] = binds
    if read_only_build and not repair_sandbox.disabled():
        boundary = wrapped.index("--")
        for build in [worktree / ".lake/build", *worktree.glob(".lake/packages/*/.lake/build")]:
            if build.is_dir():
                wrapped[boundary:boundary] = ["--ro-bind", str(build.resolve()), str(build.resolve())]
                boundary += 3
    started = time.monotonic()
    started_at = time.time()
    peak = 0
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("ab") as handle:
        handle.write(f"$ {' '.join(str(a) for a in argv)}\n".encode())
        handle.flush()
        start = handle.tell()
        try:
            with subprocess.Popen(
                wrapped, cwd=worktree, env=run_env, stdout=handle, stderr=subprocess.STDOUT, start_new_session=True
            ) as proc:
                try:
                    while True:
                        peak = max(peak, process_tree_rss(proc.pid))
                        if timeout is not None and time.monotonic() - started >= timeout:
                            raise subprocess.TimeoutExpired(wrapped, timeout)
                        try:
                            rc = proc.wait(timeout=0.2)
                            break
                        except subprocess.TimeoutExpired:
                            pass
                finally:
                    # Also collect a failed sibling; no check child outlives this task.
                    with contextlib.suppress(ProcessLookupError):
                        os.killpg(proc.pid, signal.SIGKILL)
                    proc.wait()
        except subprocess.TimeoutExpired:
            rc = 124
        except OSError as error:
            handle.write(f"{error}\n".encode())
            rc = 127
    with log.open("rb") as handle:
        handle.seek(start)
        output = handle.read().decode("utf-8", errors="replace")
    return {
        "rc": rc,
        "sampled_peak_rss_bytes": peak,
        "seconds": round(time.monotonic() - started, 1),
        "started_at": started_at,
        "finished_at": time.time(),
        "log": str(log),
        "output": output,
    }


def process_tree_rss(pid: int) -> int:
    """Best-effort sampled RSS, including bwrap and all descendants; telemetry only."""
    total = 0
    pending = [pid]
    seen = set()
    while pending:
        child = pending.pop()
        if child in seen:
            continue
        seen.add(child)
        try:
            status = Path(f"/proc/{child}/status").read_text()
            total += next(int(line.split()[1]) * 1024 for line in status.splitlines() if line.startswith("VmRSS:"))
            # Linux attributes each fork to the spawning thread. Lake starts
            # executables from its task pool, so the leader's children file alone
            # misses the audit/lint processes that consume most of the memory.
            for children in Path(f"/proc/{child}/task").glob("*/children"):
                with contextlib.suppress(OSError, ValueError):
                    pending.extend(int(n) for n in children.read_text().split())
        except (OSError, ValueError, StopIteration):
            continue
    return total


def _driver_key(worktree: Path, ref: str) -> str:
    digest = hashlib.sha256(ref.encode())
    # A pristine driver root (`pristine_driver_root`) also names the seed packages it builds against.
    for name in ("lean-toolchain", "lake-manifest.json", *((SEED_FILE,) if (worktree / SEED_FILE).is_file() else ())):
        path = worktree / name
        digest.update(path.read_bytes() if path.is_file() else b"-")
    return digest.hexdigest()


DRIVER_SCRIPT = (
    'bash "$1/build-lint-driver.sh" "$2" "$1/LintEnvDriver.lean" "$3" && bash "$1/build-audit-driver.sh" "$2" "$1" "$3"'
)
DRIVER_EXES = ("lint-env-driver", "audit-driver")


def toolchain_executable(worktree: Path, env: dict, name: str) -> str | None:
    """The absolute path of ``name`` in the toolchain the worktree's ``lean-toolchain`` names, under elan's
    read-only toolchains, or None."""
    try:
        pin = (worktree / "lean-toolchain").read_text().strip()
    except OSError:
        return None
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+:[A-Za-z0-9_.+-]+", pin):
        return None
    directory = pin.replace("/", "--").replace(":", "---")
    path = repair_sandbox._elan_home(env) / "toolchains" / directory / "bin" / name
    return str(path) if path.is_file() and not path.is_symlink() else None


def pristine_driver_root(worktree: Path, tools_root: Path, ref: str, env: dict) -> Path | None:
    """A project root for the driver builds that candidate code has never written, or None: only the
    toolchain pin (copied) and the worker's shared seed packages, under ``tools_root/drivers/root-<key>``.
    The packages are the target the worker itself recorded when it linked the worktree's ``.lake/packages``
    (``repair_sandbox.linked_packages``; the link itself is build state and never followed), and only one
    inside a configured read-only tree, never another worktree's ``.lake``. Staging builds its drivers
    before any candidate code runs; this is the worker's equivalent."""
    recorded = repair_sandbox.linked_packages(worktree)
    if len(recorded) != 1:
        return None
    shared = recorded[0]
    if shared != shared.resolve() or not any(shared.is_relative_to(t) for t in repair_sandbox._read_only_trees(env)):
        return None
    if not (shared / "batteries" / "Batteries" / "Tactic" / "Lint.lean").is_file():
        return None
    try:
        toolchain = (worktree / "lean-toolchain").read_text()
        manifest = (
            (worktree / "lake-manifest.json").read_text() if (worktree / "lake-manifest.json").is_file() else None
        )
        batteries = (
            (shared / "batteries" / "lake-manifest.json").read_bytes()
            if (shared / "batteries" / "lake-manifest.json").is_file()
            else b""
        )
    except OSError:
        return None
    # The cached drivers are keyed by the pin, the toolchain, the dependency manifest and the seed itself
    # (its path and Batteries' own manifest), so a changed seed never reuses an older driver.
    # "pool-free": drivers built before decision 202 could restore outputs from a pool; none is reused.
    seed = f"{shared}\n{hashlib.sha256(batteries).hexdigest()}\npool-free\n"
    root = (
        tools_root
        / DRIVERS_DIR
        / f"root-{hashlib.sha256((_driver_key(worktree, ref) + seed).encode()).hexdigest()[:24]}"
    )
    lake_dir = root / ".lake"
    lake_dir.mkdir(parents=True, exist_ok=True)
    (root / "lean-toolchain").write_text(toolchain)
    if manifest is not None:
        (root / "lake-manifest.json").write_text(manifest)
    (root / SEED_FILE).write_text(seed)
    link = lake_dir / "packages"
    if link.is_symlink() and link.resolve() != shared:
        link.unlink()
    if not link.is_symlink():
        link.symlink_to(shared)
    return root


def build_drivers(
    worktree: Path,
    scripts: Path,
    env: dict,
    ref: str,
    log: Path,
    timeout: float | None,
    *,
    tools_root: Path | None = None,
) -> tuple[Path, dict | None]:
    """Compile ``lint-env-driver`` and ``audit-driver`` with the pinned ``build-lint-driver.sh`` and
    ``build-audit-driver.sh``, in the sandbox; reused while pin, toolchain and manifest are unchanged.

    With ``tools_root`` (the checks' verifier, decision 200) they live in ``tools_root/drivers/<key>``,
    outside every candidate-writable directory: built in a scratch directory that is the build's only
    writable bind besides the worktree's ``.lake``, with ``scripts`` read-only, then moved into place,
    and never written again. Without it (the agent's advisory targeted lint) they stay in the worktree."""
    key = _driver_key(worktree, ref)
    if tools_root is None:
        drivers = worktree / WORK_DIR / "drivers"
        stamp = drivers / "key"
        if stamp.is_file() and stamp.read_text() == key and all(os.access(drivers / e, os.X_OK) for e in DRIVER_EXES):
            return drivers, None
        shutil.rmtree(drivers, ignore_errors=True)
        drivers.mkdir(parents=True)
        # `check` materializes the scripts in its host-only verifier directory, outside the worktree:
        # without the bind the sandbox cannot see them (bash exits 127). The targeted lint keeps them
        # in the worktree, which the sandbox already shows.
        outside = not scripts.resolve().is_relative_to(worktree.resolve())
        run = _sandboxed(
            ["bash", "-c", DRIVER_SCRIPT, "build-drivers", scripts, worktree, drivers],
            worktree,
            env,
            None,
            log,
            timeout,
            ro_binds=(scripts,) if outside else (),
        )
        if run["rc"] != 0 or not all(os.access(drivers / e, os.X_OK) for e in DRIVER_EXES):
            raise Unavailable(f"building the audit and lint drivers exited {run['rc']}: {_tail(run['output'])}")
        stamp.write_text(key)
        return drivers, {k: v for k, v in run.items() if k != "output"}
    dest = tools_root / DRIVERS_DIR / key

    def regular(directory: Path) -> bool:
        """Every driver is a regular executable file, not a link (which could lead into the worktree)."""
        return all(
            (directory / e).is_file() and not (directory / e).is_symlink() and os.access(directory / e, os.X_OK)
            for e in DRIVER_EXES
        )

    if regular(dest):
        return dest, None
    dest.parent.mkdir(parents=True, exist_ok=True)
    scratch = Path(tempfile.mkdtemp(prefix=f".{key[:12]}-", dir=dest.parent))
    try:
        run = _sandboxed(
            ["bash", "-c", DRIVER_SCRIPT, "build-drivers", scripts, worktree, scratch],
            worktree,
            pool_free(env),
            None,
            log,
            timeout,
            ro_binds=(scripts,),
            rw_binds=(scratch,),
        )
        if run["rc"] != 0 or not regular(scratch):
            raise Unavailable(f"building the audit and lint drivers exited {run['rc']}: {_tail(run['output'])}")
        try:
            os.rename(scratch, dest)
        except OSError:
            if not regular(dest):  # another check's rename did not win either
                raise
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    return dest, {k: v for k, v in run.items() if k != "output"}


def pool_free(env: dict) -> dict:
    """``env`` without the task's artifact pool, for the verifier's own driver builds (decision 202).

    The drivers are kept under ``tools_root`` and reused by every later batch's checks, but the pool is
    the batch's and candidate code can write it: a driver build that restored an output from it could
    carry one batch's code into every later batch's verifier. Lake's artifact cache is off, so the
    build reads only the pristine root and the read-only seed packages, and no pool is mounted."""
    clean = {k: v for k, v in env.items() if k not in ("LAKE_CACHE_DIR", "LAKE_RESTORE_ARTIFACTS")}
    clean["LAKE_ARTIFACT_CACHE"] = "false"
    return clean


# ----------------------------------------------------------------------------------------------
# The checks


def changed_modules(worktree: Path, base: str) -> list[str]:
    """``lint-scope.sh``'s module list: the TauCeti modules changed between ``base`` and HEAD (no rename
    detection, deletions left out)."""
    proc = _run(
        ["git", "-C", worktree, "diff", "--no-ext-diff", "--no-textconv", "--no-renames", "--name-only", "-z"]
        + ["--diff-filter=d", base, "HEAD", "--"],
        timeout=120,
    )
    if proc.returncode != 0:
        raise Unavailable(f"the lint scope: git diff {base[:12]} HEAD: {_tail(proc.stderr)}")
    files = [f for f in proc.stdout.split("\0") if MODULE_PATH.match(f)]
    return sorted({f[: -len(".lean")].replace("/", ".") for f in files})


def findings_of(kind: str, name: str, run: dict) -> list[dict]:
    """The ``kind`` findings ``diagnostics.parse_log`` reads from a check's output (with the style lint's
    header findings); a nonzero exit with none is one finding carrying the output's tail."""
    lines = run["output"].splitlines()
    found = [d for d in diagnostics.parse_log(lines) if d["kind"] == kind]
    if name == "lint-style":
        found += diagnostics.header_style_findings(lines)
    if run["rc"] != 0 and not found:
        found = [{"kind": kind, "module": "", "message": f"{name} exited {run['rc']}:\n{_tail(run['output'])}"}]
    return found


def driver_failure(run: dict, findings: list[dict]) -> bool:
    """Whether an environment-lint run ended in ``lint-env.sh``'s driver failure with no finding that
    names a module (such as the docstring-scan driver failing to compile its module list)."""
    if run["rc"] == 0 or any(f.get("module") for f in findings):
        return False
    return any(DRIVER_FAILURE in line for line in run["output"].splitlines())


def check(
    worktree: Path,
    env: dict,
    *,
    base: str,
    tools_root: Path,
    logdir: Path,
    lake: tuple[str, ...] = ("lake",),
    timeout: float | None = None,
) -> dict:
    """Staging's checks over the environment already built in ``worktree``, all at once; the caller
    holds the build lock. These are the audit driver (import, duplicates, axioms), the module-system
    check, the environment lint, the style lint and the dot-notation lint. The environment lint covers
    the modules changed since ``base`` first and the whole library once nothing else is found, so
    ``lint.scope`` is ``changed`` only on a failed round. ``status`` is ``passed``, ``failed``
    (``diagnostics`` lists every check's findings in the controller's shape) or ``unavailable``
    (``reason``)."""
    verifier_root = tools_root / VERIFIER_DIR
    verifier_root.mkdir(parents=True, exist_ok=True)
    verifier = Path(tempfile.mkdtemp(prefix="check-", dir=verifier_root))
    try:
        return _check(
            worktree,
            env,
            base=base,
            tools_root=tools_root,
            logdir=logdir,
            lake=lake,
            timeout=timeout,
            verifier=verifier,
        )
    finally:
        shutil.rmtree(verifier, ignore_errors=True)


def _check(
    worktree: Path,
    env: dict,
    *,
    base: str,
    tools_root: Path,
    logdir: Path,
    lake: tuple[str, ...],
    timeout: float | None,
    verifier: Path,
) -> dict:
    """`check`, with ``verifier`` the host-only directory for this check's scripts and scope files."""
    started = time.monotonic()
    record: dict = {"status": "passed", "diagnostics": []}
    work = worktree / WORK_DIR
    # The phase markers (and, without shared packages, the drivers) stay under `.lake`, which candidate code
    # can write. A link it left in place of the directory is removed rather than followed.
    if work.is_symlink():
        work.unlink()
    work.mkdir(parents=True, exist_ok=True)
    setup_started = time.monotonic()
    record["sandbox"] = not repair_sandbox.disabled(env)
    try:
        # The pin and policy files come from the task's policy commit (`main`), as staging takes them,
        # when the controller named one; otherwise, and when it cannot be read, from the worktree.
        source = worktree
        policy_commit = (read_settings(worktree) or {}).get("policy_commit")
        if policy_commit:
            try:
                source = policy_tree(worktree, policy_commit, tools_root)
                record["policy_commit"] = policy_commit
            except Unavailable as error:
                record["policy_error"] = str(error)
        try:
            repo, ref = tools_pin(source)
            record["tools"] = f"{repo}@{ref}"
            checkout = tools_checkout(tools_root, repo, ref)
            ref = checkout.name  # the exact commit, which keys the compiled drivers
            record["tools_commit"] = f"{repo}@{ref}"
            record["tools_seconds"] = round(time.monotonic() - setup_started, 3)
            materialize_started = time.monotonic()
            scripts = materialize(checkout, source, verifier / "scripts")
        finally:
            if source != worktree:
                shutil.rmtree(source, ignore_errors=True)
        record["materialize_seconds"] = round(time.monotonic() - materialize_started, 3)
        drivers_started = time.monotonic()
        pristine = pristine_driver_root(worktree, tools_root, ref, env)
        if pristine is None:
            # The worktree has no shared read-only packages: build as before, and say the verifier is not isolated.
            drivers, built = build_drivers(worktree, scripts, env, ref, logdir / "audit-drivers.log", timeout)
            record["drivers_isolated"] = False
        else:
            drivers, built = build_drivers(
                pristine, scripts, env, ref, logdir / "audit-drivers.log", timeout, tools_root=tools_root
            )
            record["drivers_isolated"] = True
        record["drivers_seconds"] = round(time.monotonic() - drivers_started, 3)
        if built:
            record["drivers"] = built
    except (Unavailable, repair_sandbox.SandboxUnavailable, OSError) as error:
        record.update(status="unavailable", reason=str(error), seconds=round(time.monotonic() - started, 1))
        return record
    from tauceti_queue import repair_publish

    publication = repair_publish.start_early(worktree, env)
    if publication is not None:
        record["publication"] = publication
    phases_path = work / "audit-phases.tsv"
    phase_record = logdir / ("audit-phases-" + str(time.time_ns()) + ".tsv")
    with contextlib.suppress(OSError):
        phases_path.unlink(missing_ok=True)
        logdir.mkdir(parents=True, exist_ok=True)
        phase_record.touch(exist_ok=False)
        # The driver can write only inside .lake. A hardlink preserves its phase
        # markers in task logs even if the parent/helper is killed mid-audit.
        os.link(phase_record, phases_path)
    record["audit_phases_file"] = str(phase_record)
    # `lake env` puts the worktree's build directories first on PATH, which the candidate can write: name the
    # trusted executables by absolute path, as staging's sandbox-build.sh does with `timeout`.
    timeout_exe = shutil.which("timeout") or "timeout"
    lean_exe = toolchain_executable(worktree, env, "lean")
    # Inside the sandbox only the read-only system roots are where the host has them.
    system = os.path.isabs(timeout_exe) and any(
        Path(timeout_exe).is_relative_to(root) for root in repair_sandbox.SYSTEM_ROOTS
    )
    if not system or lean_exe is None:
        record["verifier_isolated_reason"] = "the trusted timeout or lean executable could not be resolved"
    audit_argv = [*lake, "env", timeout_exe, "--signal=TERM", "--kill-after=30", f"{AUDIT_TIMEOUT_SECONDS}s"]
    project = {"TAUCETI_PROJECT_ROOT": worktree, "TAUCETI_TRUSTED_SCRIPTS": scripts, "LINT_CHANGED_ONLY": "0"}
    full_lint_env = {
        **project,
        "LINT_DRIVER_EXE": drivers / "lint-env-driver",
        "LINT_ONLY_MODULES": "",
        "LINT_SHARDS": "1" if repair_sandbox.disabled() else LINT_SHARDS,
        "TAUCETI_LINT_PROFILE": env.get("TAUCETI_LINT_PROFILE", "0"),
    }
    # The first lint covers the changed modules (staging's PR preflight scope). It misses findings
    # in unchanged declarations, such as a new simp lemma breaking an old one's simp normal form,
    # so a round that finds nothing ends with the whole-library lint below.
    lint_env = full_lint_env
    try:
        modules = changed_modules(worktree, base)
    except (Unavailable, OSError, subprocess.SubprocessError):
        modules = None
    if modules is not None and len(modules) > CHANGED_LINT_MODULE_LIMIT:
        # Too many for the changed-only driver: the whole-library lint (CI's own scope) covers them.
        record["changed_lint"] = {"skipped": "module_limit", "modules": len(modules)}
        modules = None
    if modules is not None:
        scope_file = verifier / "lint-modules.txt"
        scope_file.write_text("".join(m + "\n" for m in modules))
        lint_env = {**full_lint_env, "LINT_ONLY_MODULES": scope_file, "LINT_CHANGED_ONLY": "1"}
    dot_notation = [
        "python3",
        scripts / "lint-dot-notation.py",
        "--baseline",
        scripts / "lint-dot-notation-baseline.txt",
    ]
    dot_notation += ["--mathlib-root", ".lake/packages/mathlib/Mathlib", "--source-root", "TauCeti"]
    # (record key, finding kind, argv, sandbox environment, log, build outputs read-only). The
    # style lint may build Mathlib's lint-style executable, so its build outputs stay writable.
    jobs = [
        (
            "audit",
            "audit",
            [*audit_argv, drivers / "audit-driver"],
            {"AUDIT_PHASES_FILE": phases_path},
            "audit.log",
            True,
        ),
        ("lint", "lint", ["bash", scripts / "lint-env.sh"], lint_env, "lint-env.log", True),
        (
            "module_system",
            "audit",
            [*lake, "env", lean_exe or "lean", "--run", scripts / "ModuleSystem.lean"],
            None,
            "module-system.log",
            True,
        ),
        ("style_lint", "lint", ["bash", scripts / "lint-style.sh"], project, "lint-style.log", False),
        ("dot_notation", "lint", dot_notation, None, "lint-dot-notation.log", False),
    ]
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(jobs)) as pool:
        futures = [
            pool.submit(
                _sandboxed,
                argv,
                worktree,
                env,
                extra,
                logdir / log,
                timeout,
                read_only_build=read_only,
                ro_binds=(verifier, drivers),
            )
            for _, _, argv, extra, log, read_only in jobs
        ]
        runs = {job[0]: future.result() for job, future in zip(jobs, futures, strict=True)}
    # Read the markers through the host-only hard link in the log directory, never through the path under
    # `.lake`: candidate code could have replaced that with a link to any host file meanwhile.
    record["audit_phases"] = audit_phases(phase_record, runs["audit"])
    with contextlib.suppress(OSError):
        shutil.copyfile(phase_record, logdir / "audit-phases.tsv")
    found = {name: findings_of(kind, log.removesuffix(".log"), runs[name]) for name, kind, _, _, log, _ in jobs}
    scope = "full"
    if modules is not None:
        scope = "changed"
        # A changed-only run that failed in its own driver, naming no module, gives the repair
        # nothing to fix; the whole-library lint replaces it whatever the other checks found.
        driver_failed = driver_failure(runs["lint"], found["lint"])
        if driver_failed or not any(found.values()):
            record["changed_lint"] = {k: v for k, v in runs["lint"].items() if k != "output"} | {
                "modules": len(modules)
            }
            if driver_failed:
                record["changed_lint"]["skipped"] = "driver_failure"
            runs["lint"] = _sandboxed(
                ["bash", scripts / "lint-env.sh"],
                worktree,
                env,
                full_lint_env,
                logdir / "lint-env.log",
                timeout,
                read_only_build=True,
                ro_binds=(verifier, drivers),
            )
            found["lint"] = findings_of("lint", "lint-env", runs["lint"])
            scope = "full"
    for name, *_ in jobs:
        record[name] = {k: v for k, v in runs[name].items() if k != "output"} | {"findings": len(found[name])}
        record["diagnostics"] += found[name]
    record["lint"]["scope"] = scope
    # The scripts, scope files and drivers lived outside the worktree and were mounted read-only into
    # every check (decision 200); without the sandbox nothing kept the candidate's code away from them.
    record["verifier_isolated"] = (
        record["sandbox"] and record.get("drivers_isolated") is True and "verifier_isolated_reason" not in record
    )
    record["status"] = "failed" if record["diagnostics"] else "passed"
    record["seconds"] = round(time.monotonic() - started, 1)
    with contextlib.suppress(OSError):
        (logdir / "checks-timing.json").write_text(
            json.dumps({k: v for k, v in record.items() if k != "diagnostics"}, indent=2) + "\n"
        )
    return record


def audit_phases(path: Path, run: dict) -> list[dict]:
    """Best-effort wall times from the pinned driver's epoch phase markers.

    Missing or malformed telemetry never changes a verdict. Incomplete runs retain
    their observed intervals, marked incomplete, rather than claiming checks passed.
    """
    events = []
    try:
        for line in path.read_text().splitlines():
            name, stamp = line.split("\t")
            stamp = float(stamp)
            if name not in ("audit-import", "audit-duplicates", "audit-axioms"):
                return []
            if not run["started_at"] <= stamp <= run["finished_at"]:
                continue
            if events and stamp < events[-1][1]:
                return []
            events.append((name, stamp))
    except (OSError, ValueError, KeyError):
        return []
    return [
        {
            "name": name,
            "started_at": stamp,
            "seconds": round((events[index + 1][1] if index + 1 < len(events) else run["finished_at"]) - stamp, 3),
            "complete": index + 1 < len(events) or run["rc"] == 0,
        }
        for index, (name, stamp) in enumerate(events)
    ]


def append_errors(directory: Path, found: list[dict], owner=None) -> None:
    """Append findings to ``directory/errors.jsonl`` as the streaming build writes compiler errors:
    ``kind``, ``module``, ``file``, ``message`` and, with ``owner`` (path to ``{member, commit}``), the
    member whose commit last touched the file."""
    if not found:
        return
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / ERRORS_FILE).open("a") as handle:
        for finding in found:
            path = finding.get("path") or ""
            entry = {
                "kind": finding["kind"],
                "module": finding.get("module") or "",
                "file": path,
                "line": finding.get("line_number") or 0,
                "column": 0,
                "message": finding["message"],
            }
            if owner is not None and path:
                entry.update(owner(path))
            handle.write(json.dumps(entry) + "\n")


def verify(
    worktree: Path,
    env: dict,
    *,
    base: str,
    tools_root: Path,
    logdir: Path,
    lake: tuple[str, ...] = ("lake",),
    timeout: float | None = None,
    lock: Path | None = None,
    audits: bool = True,
) -> dict:
    """The worker's verification: hold the build lock, run the whole-library ``lake build --iofail`` in
    the sandbox and, when it is green and ``audits``, ``check``. ``ok`` is false when the build failed or a
    check reported a finding; ``diagnostics`` are the findings in the controller's shape (compiler errors
    of a failed build, else the checks'), also appended to ``.tauceti-repair/errors.jsonl``."""
    started = time.monotonic()
    record: dict = {"ok": True, "diagnostics": []}
    lock = lock or worktree / repair_tools.TOOLS_DIR / repair_tools.LOCK_DIR
    repair_tools.take_lock(lock, log=lambda *a, **k: None)
    try:
        from tauceti_queue.repair_publish import build_flags

        build = _sandboxed(
            [*lake, "build", *build_flags(worktree)], worktree, env, None, logdir / "verify-build.log", timeout
        )
        record["build"] = {k: v for k, v in build.items() if k != "output"}
        if build["rc"] != 0:
            found = [d for d in diagnostics.parse_log(build["output"].splitlines()) if d["kind"] == "compiler"]
            record["diagnostics"] = found or [
                {
                    "kind": "compiler",
                    "module": "",
                    "message": f"lake build --iofail exited {build['rc']}:\n{_tail(build['output'])}",
                }
            ]
            record["ok"] = False
        elif audits:
            checks = check(
                worktree,
                env,
                base=base,
                tools_root=tools_root,
                logdir=logdir,
                lake=lake,
                timeout=timeout,
            )
            record["checks"] = {k: v for k, v in checks.items() if k != "diagnostics"}
            record["diagnostics"] = checks["diagnostics"]
            record["ok"] = checks["status"] != "failed"
        if record["diagnostics"]:
            append_errors(worktree / repair_tools.TOOLS_DIR, record["diagnostics"])
    finally:
        shutil.rmtree(lock, ignore_errors=True)
    record["seconds"] = round(time.monotonic() - started, 1)
    return record


def format_findings(found: list[dict]) -> str:
    return "\n".join(f"{d['kind']} {d.get('path') or d.get('module') or '-'}: {d['message']}" for d in found)


def helper_checks(root: Path) -> int:
    """The checks after a green whole-library build of ``.tauceti-repair/build`` (the caller holds the lock):
    print the findings, append them to ``errors.jsonl`` and return 1 when there are any."""
    settings = read_settings(root)
    if settings is None:
        print("audits: not configured in this worktree (.tauceti-repair/audit.json is missing); skipped", flush=True)
        return 0
    logdir = (
        Path(settings["logdir"]) / ("helper-audit-" + str(time.time_ns()))
        if settings.get("logdir")
        else root / repair_tools.TOOLS_DIR / "audit"
    )
    print(
        "audits: running staging's audit, module-system check and environment, style and dot-notation lints", flush=True
    )
    result = check(
        root,
        dict(os.environ),
        base=settings["base"],
        tools_root=Path(settings["tools_root"]),
        logdir=logdir,
        lake=tuple(settings.get("lake") or ("lake",)),
    )
    if result["status"] == "unavailable":
        print(f"audits: could not run ({result['reason']})", flush=True)
        return 0
    if result["status"] == "passed":
        print("audits: passed", flush=True)
        return 0
    append_errors(root / repair_tools.TOOLS_DIR, result["diagnostics"])
    print(
        f"audits: {len(result['diagnostics'])} finding(s), also appended to .tauceti-repair/errors.jsonl:", flush=True
    )
    print(format_findings(result["diagnostics"]), flush=True)
    return 1

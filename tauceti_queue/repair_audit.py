"""Worker-owned whole-library verification using the candidate's pinned CI tools.

Repairs, preparations and rebases receive a final build followed by the native
compiled audit and lint drivers. Without frozen GitHub lint-scope policy, use
staging's conservative full-library fallback. Findings return to the existing
repair loop; staging remains the authoritative landing verdict.

Measure check phases and sampled process-tree RSS in existing task logs. A
matching previous measurement and ample host/cgroup headroom permit two checks
to overlap over read-only compiled outputs under the caller's build lock.
Unmeasured or unsafe runs stay serial. Every check is joined before cleanup.
"""

from __future__ import annotations

import concurrent.futures
import contextlib
import functools
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
SETTINGS_FILE = "audit.json"  # in .tauceti-repair: what the agent's build script needs to run the checks
ERRORS_FILE = "errors.jsonl"  # repair_stream's error list, in .tauceti-repair
TOOLS_DIR = "tauceti-tools"  # under the worker's root: one TauCetiTools checkout per pinned ref
URL_VARIABLE = "TAUCETI_QUEUE_TOOLS_URL"  # a ``{repo}`` template; tests point it at a local repository
DEFAULT_URL = "https://github.com/{repo}.git"
ENABLE_VARIABLE = "TAUCETI_QUEUE_WORKER_AUDITS"
AUDIT_TIMEOUT_SECONDS = 3000  # sandbox-build.sh's liveness budget for the audit driver
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
    worktree: Path, *, base: str, tools_root: Path, lake: tuple[str, ...] = ("lake",), logdir: Path | None = None
) -> Path:
    """``.tauceti-repair/audit.json``: the base the lint scope is taken against, where the TauCetiTools
    checkouts live and the Lake command, for ``.tauceti-repair/build``."""
    path = worktree / repair_tools.TOOLS_DIR / SETTINGS_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    settings = {"base": base, "tools_root": str(tools_root), "lake": list(lake)}
    if logdir is not None:
        settings["logdir"] = str(logdir)
    path.write_text(json.dumps(settings))
    return path


def read_settings(worktree: Path) -> dict | None:
    try:
        data = json.loads((worktree / repair_tools.TOOLS_DIR / SETTINGS_FILE).read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and data.get("base") and data.get("tools_root") else None


# ----------------------------------------------------------------------------------------------
# The pinned TauCetiTools scripts


def tools_pin(worktree: Path) -> tuple[str, str]:
    """``(repo, ref)`` of ``[tools.tools]`` in the worktree's ``tauceti.toml``."""
    path = worktree / "tauceti.toml"
    if not path.is_file():
        raise Unavailable("the worktree has no tauceti.toml naming the TauCetiTools pin")
    try:
        data = tomllib.loads(path.read_text())
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise Unavailable(f"tauceti.toml: {error}") from error
    tools = (data.get("tools") or {}).get("tools") or {}
    repo, ref = str(tools.get("repo") or ""), str(tools.get("ref") or "")
    if not REPO.match(repo) or not SHA.match(ref):
        raise Unavailable(f"tauceti.toml: [tools.tools] needs repo and a 40-hex ref, found {repo!r} {ref!r}")
    return repo, ref


def _run(argv: list, **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run([str(a) for a in argv], text=True, capture_output=True, check=False, **kwargs)


def _tail(text: str) -> str:
    return "\n".join(text.strip().splitlines()[-TAIL_LINES:])[-TAIL_CHARS:]


def tools_checkout(tools_root: Path, repo: str, ref: str) -> Path:
    """``tools_root/<ref>``, a checkout of TauCetiTools at ``ref``, fetched on first use."""
    dest = tools_root / ref
    if (dest / "tauceti_tools").is_dir():
        return dest
    tools_root.mkdir(parents=True, exist_ok=True)
    scratch = Path(tempfile.mkdtemp(prefix=f".{ref[:12]}-", dir=tools_root))
    url = os.environ.get(URL_VARIABLE, DEFAULT_URL).format(repo=repo)
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
) -> dict:
    """``argv`` in the build sandbox with ``extra`` set inside it; output to ``log``."""
    extra = {k: str(v) for k, v in (extra or {}).items()}
    run_env = {**env, **extra}
    wrapped = repair_sandbox.sandboxed_lake([str(a) for a in argv], worktree, run_env, extra_env=extra)
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
            pending.extend(int(n) for n in Path(f"/proc/{child}/task/{child}/children").read_text().split())
        except (OSError, ValueError, StopIteration):
            continue
    return total


def available_memory() -> int:
    """Host and capped ancestor headroom; unknown measurements disable overlap."""
    try:
        available = next(
            int(line.split()[1]) * 1024
            for line in Path("/proc/meminfo").read_text().splitlines()
            if line.startswith("MemAvailable:")
        )
        relative = next(
            line.split(":", 2)[2]
            for line in Path("/proc/self/cgroup").read_text().splitlines()
            if line.startswith("0::")
        )
        root = Path("/sys/fs/cgroup")
        group = root / relative.lstrip("/")
        for ancestor in (group, *group.parents):
            if not ancestor.is_relative_to(root):
                break
            maximum = (ancestor / "memory.max").read_text().strip()
            if maximum != "max":
                available = min(available, int(maximum) - int((ancestor / "memory.current").read_text()))
        return max(available, 0)
    except (OSError, ValueError, StopIteration):
        return 0


def can_overlap(logdir: Path, key: str) -> bool:
    if repair_sandbox.disabled():
        return False  # output immutability requires the sandbox
    try:
        previous = json.loads((logdir / "checks-timing.json").read_text())
        peaks = [previous[name]["sampled_peak_rss_bytes"] for name in ("audit", "lint")]
        return (
            previous.get("driver_key") == key
            and all(p > 0 for p in peaks)
            and available_memory() > 2 * sum(peaks) + 8 * 1024**3
        )
    except (OSError, ValueError, KeyError, TypeError):
        return False


def _driver_key(worktree: Path, ref: str) -> str:
    digest = hashlib.sha256(ref.encode())
    for name in ("lean-toolchain", "lake-manifest.json"):
        path = worktree / name
        digest.update(path.read_bytes() if path.is_file() else b"-")
    return digest.hexdigest()


def build_drivers(
    worktree: Path, scripts: Path, env: dict, ref: str, log: Path, timeout: float | None
) -> tuple[Path, dict | None]:
    """Compile ``lint-env-driver`` and ``audit-driver`` with the pinned ``build-lint-driver.sh`` and
    ``build-audit-driver.sh``, in the sandbox; reused while pin, toolchain and manifest are unchanged."""
    drivers = worktree / WORK_DIR / "drivers"
    stamp, key = drivers / "key", _driver_key(worktree, ref)
    exes = (drivers / "lint-env-driver", drivers / "audit-driver")
    if stamp.is_file() and stamp.read_text() == key and all(os.access(e, os.X_OK) for e in exes):
        return drivers, None
    shutil.rmtree(drivers, ignore_errors=True)
    drivers.mkdir(parents=True)
    script = 'bash "$1/build-lint-driver.sh" "$2" "$1/LintEnvDriver.lean" "$3" && bash "$1/build-audit-driver.sh" "$2" "$1" "$3"'
    run = _sandboxed(
        ["bash", "-c", script, "build-drivers", scripts, worktree, drivers], worktree, env, None, log, timeout
    )
    if run["rc"] != 0 or not all(os.access(e, os.X_OK) for e in exes):
        raise Unavailable(f"building the audit and lint drivers exited {run['rc']}: {_tail(run['output'])}")
    stamp.write_text(key)
    return drivers, {k: v for k, v in run.items() if k != "output"}


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
    """The ``kind`` findings ``diagnostics.parse_log`` reads from a check's output; a nonzero exit with
    none is one finding carrying the output's tail."""
    found = [d for d in diagnostics.parse_log(run["output"].splitlines()) if d["kind"] == kind]
    if run["rc"] != 0 and not found:
        found = [{"kind": kind, "module": "", "message": f"{name} exited {run['rc']}:\n{_tail(run['output'])}"}]
    return found


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
    """CI's audits and full environment lint, over the environment already built
    in ``worktree``; the caller holds the build lock. ``status`` is ``passed``, ``failed`` (``diagnostics``
    lists the findings in the controller's shape) or ``unavailable`` (``reason``)."""
    started = time.monotonic()
    record: dict = {"status": "passed", "diagnostics": []}
    from tauceti_queue import repair_publish

    publication = repair_publish.start_early(worktree, env)
    if publication is not None:
        record["publication"] = publication
    work = worktree / WORK_DIR
    setup_started = time.monotonic()
    try:
        repo, ref = tools_pin(worktree)
        record["tools"] = f"{repo}@{ref}"
        checkout = tools_checkout(tools_root, repo, ref)
        record["tools_seconds"] = round(time.monotonic() - setup_started, 3)
        materialize_started = time.monotonic()
        scripts = materialize(checkout, worktree, work / "scripts")
        record["materialize_seconds"] = round(time.monotonic() - materialize_started, 3)
        drivers_started = time.monotonic()
        drivers, built = build_drivers(worktree, scripts, env, ref, logdir / "audit-drivers.log", timeout)
        record["drivers_seconds"] = round(time.monotonic() - drivers_started, 3)
        if built:
            record["drivers"] = built
    except (Unavailable, repair_sandbox.SandboxUnavailable, OSError) as error:
        record.update(status="unavailable", reason=str(error), seconds=round(time.monotonic() - started, 1))
        return record
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
    audit_argv = [*lake, "env", "timeout", "--signal=TERM", "--kill-after=30", f"{AUDIT_TIMEOUT_SECONDS}s"]
    audit_job = functools.partial(
        _sandboxed,
        [*audit_argv, drivers / "audit-driver"],
        worktree,
        env,
        {"AUDIT_PHASES_FILE": phases_path},
        logdir / "audit.log",
        timeout,
        read_only_build=True,
    )
    # The task has no frozen GitHub label/branch policy that could justify scoped
    # lint. Use staging's conservative full-library fallback, including unchanged
    # declarations affected by new simp lemmas or other global environment changes.
    extra = {
        "TAUCETI_PROJECT_ROOT": worktree,
        "TAUCETI_TRUSTED_SCRIPTS": scripts,
        "LINT_DRIVER_EXE": drivers / "lint-env-driver",
        "LINT_ONLY_MODULES": "",
        "LINT_CHANGED_ONLY": "0",
        "TAUCETI_LINT_PROFILE": env.get("TAUCETI_LINT_PROFILE", "0"),
    }
    lint_job = functools.partial(
        _sandboxed,
        ["bash", scripts / "lint-env.sh"],
        worktree,
        env,
        extra,
        logdir / "lint-env.log",
        timeout,
        read_only_build=True,
    )
    record["driver_key"] = _driver_key(worktree, ref)
    record["overlap"] = can_overlap(logdir, record["driver_key"])
    if record["overlap"]:
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as checks:
            audit_future, lint_future = checks.submit(audit_job), checks.submit(lint_job)
            audit, lint = audit_future.result(), lint_future.result()
    else:
        audit, lint = audit_job(), lint_job()
    record["audit_phases"] = audit_phases(phases_path, audit)
    with contextlib.suppress(OSError):
        if not os.path.samefile(phases_path, phase_record):
            shutil.copyfile(phases_path, phase_record)
    with contextlib.suppress(OSError):
        shutil.copyfile(phases_path, logdir / "audit-phases.tsv")
    found = findings_of("audit", "audit-driver", audit)
    record["audit"] = {k: v for k, v in audit.items() if k != "output"} | {"findings": len(found)}
    record["diagnostics"] += found
    found = findings_of("lint", "lint-env.sh", lint)
    record["lint"] = {k: v for k, v in lint.items() if k != "output"} | {"scope": "full", "findings": len(found)}
    record["diagnostics"] += found
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
            checks = check(worktree, env, base=base, tools_root=tools_root, logdir=logdir, lake=lake, timeout=timeout)
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
    print("audits: running CI's audit driver and the changed-module environment lint", flush=True)
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

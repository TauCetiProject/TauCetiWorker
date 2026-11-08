"""CI's audits and changed-module environment lint on the repair worker (``docs/redesign/specs/ai-merge-queue.md``,
improvement 13 of the improvements from the first production repair).

Head 2's first CI round after a green worker build failed in the audit phase on one duplicate declaration
(``duplicate-declarations: SimpleGraph.Walk.IsPath.start_notMem_tail_support is declared in both
TauCeti.Combinatorics.SimpleGraph.Connectivity.Menger and TauCeti.Combinatorics.SimpleGraph.Paths``) that the
agent's text scan had listed and set aside, because nothing on the worker ran the real audit. Checks are
not left to the agent: the worker runs them after every green whole-library ``lake build`` it performs
and refuses to submit while any finding remains, and the agent reads the findings as ordinary diagnostics.

CI's required build (TauCeti ``.github/workflows/pr-build.yml``, step "Build exact candidate under bwrap
with workflow-pinned audits, offline", running TauCetiTools ``sandbox-build.sh``) runs, after its green
``lake build --iofail``:

* the ``audit-driver`` (TauCetiTools ``Axioms.lean``, compiled by ``build-audit-driver.sh``) under
  ``lake env timeout 3000s``: one import of every TauCeti module (phase ``audit-import``), the per-module
  ownership check (``audit-duplicates``) and the axiom scan (``audit-axioms``);
* the environment lint ``lint-env.sh`` with the ``lint-env-driver`` (``LintEnvDriver.lean``, compiled by
  ``build-lint-driver.sh`` against the project's Batteries), on the modules ``lint-scope.sh`` selects:
  the TauCeti modules the change touches, ``git diff --no-renames --diff-filter=d`` of its base and head.

This module runs the same from the same sources. The TauCetiTools pin the worktree's ``tauceti.toml``
names (``[tools.tools] repo`` and ``ref``) is fetched once into ``<root>/tauceti-tools/<ref>`` and
materialized with ``python -m tauceti_tools materialize`` (the TauCetiTools setup action's step, with the
worktree's ``scripts/`` as the policy directory) into ``.lake/tauceti-repair-audit/scripts``. Both
drivers are compiled from it inside the sandbox (``repair_sandbox``) into
``.lake/tauceti-repair-audit/drivers``, once per worktree, pin, toolchain and manifest, and the audit and
the lint run in the sandbox over the built environment, the lint on the modules changed between the
task's base and HEAD. Their output is read with ``diagnostics.parse_log``, the controller's own reader of
CI logs, so a finding is the diagnostic CI would have produced: ``{"kind": "audit", "module", "path",
"message": "duplicate-declarations: ..."}`` once per TauCeti module named, ``lint`` with its location. A
nonzero exit without a parsed finding is one diagnostic carrying the output's tail. What could not run
at all (no pin, the fetch, the materialization or a driver build failed) is ``unavailable``: logged,
recorded, and not a finding.

Findings are appended to ``.tauceti-repair/errors.jsonl`` beside the compiler errors (``append_errors``).
CI's module-system check and source style lint are not run here.

``TAUCETI_QUEUE_WORKER_AUDITS=0`` (``--no-worker-audits``) turns the worker's verification and the
automatic audits off.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
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


def write_settings(worktree: Path, *, base: str, tools_root: Path, lake: tuple[str, ...] = ("lake",)) -> Path:
    """``.tauceti-repair/audit.json``: the base the lint scope is taken against, where the TauCetiTools
    checkouts live and the Lake command, for ``.tauceti-repair/build``."""
    path = worktree / repair_tools.TOOLS_DIR / SETTINGS_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"base": base, "tools_root": str(tools_root), "lake": list(lake)}))
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


def _sandboxed(argv: list, worktree: Path, env: dict, extra: dict | None, log: Path, timeout: float | None) -> dict:
    """``argv`` in the build sandbox with ``extra`` set inside it; output to ``log``."""
    extra = {k: str(v) for k, v in (extra or {}).items()}
    run_env = {**env, **extra}
    wrapped = repair_sandbox.sandboxed_lake([str(a) for a in argv], worktree, run_env, extra_env=extra)
    started = time.monotonic()
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("ab") as handle:
        handle.write(f"$ {' '.join(str(a) for a in argv)}\n".encode())
        handle.flush()
        start = handle.tell()
        try:
            rc = subprocess.run(
                wrapped, cwd=worktree, env=run_env, stdout=handle, stderr=subprocess.STDOUT, timeout=timeout
            ).returncode
        except subprocess.TimeoutExpired:
            rc = 124
        except OSError as error:
            handle.write(f"{error}\n".encode())
            rc = 127
    with log.open("rb") as handle:
        handle.seek(start)
        output = handle.read().decode("utf-8", errors="replace")
    return {"rc": rc, "seconds": round(time.monotonic() - started, 1), "log": str(log), "output": output}


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
    """CI's audits, then the environment lint on the changed modules, over the environment already built
    in ``worktree``; the caller holds the build lock. ``status`` is ``passed``, ``failed`` (``diagnostics``
    lists the findings in the controller's shape) or ``unavailable`` (``reason``)."""
    started = time.monotonic()
    record: dict = {"status": "passed", "diagnostics": []}
    work = worktree / WORK_DIR
    try:
        repo, ref = tools_pin(worktree)
        record["tools"] = f"{repo}@{ref}"
        checkout = tools_checkout(tools_root, repo, ref)
        scripts = materialize(checkout, worktree, work / "scripts")
        drivers, built = build_drivers(worktree, scripts, env, ref, logdir / "audit-drivers.log", timeout)
        if built:
            record["drivers"] = built
        modules = changed_modules(worktree, base)
    except (Unavailable, repair_sandbox.SandboxUnavailable, OSError) as error:
        record.update(status="unavailable", reason=str(error), seconds=round(time.monotonic() - started, 1))
        return record
    audit_argv = [*lake, "env", "timeout", "--signal=TERM", "--kill-after=30", f"{AUDIT_TIMEOUT_SECONDS}s"]
    audit = _sandboxed(
        [*audit_argv, drivers / "audit-driver"],
        worktree,
        env,
        {"AUDIT_PHASES_FILE": work / "audit-phases.tsv"},
        logdir / "audit.log",
        timeout,
    )
    found = findings_of("audit", "audit-driver", audit)
    record["audit"] = {k: v for k, v in audit.items() if k != "output"} | {"findings": len(found)}
    record["diagnostics"] += found
    if modules:
        scope = work / "lint-modules.txt"
        scope.write_text("".join(m + "\n" for m in modules))
        extra = {
            "TAUCETI_PROJECT_ROOT": worktree,
            "TAUCETI_TRUSTED_SCRIPTS": scripts,
            "LINT_DRIVER_EXE": drivers / "lint-env-driver",
            "LINT_ONLY_MODULES": scope,
        }
        lint = _sandboxed(["bash", scripts / "lint-env.sh"], worktree, env, extra, logdir / "lint-env.log", timeout)
        found = findings_of("lint", "lint-env.sh", lint)
        record["lint"] = {k: v for k, v in lint.items() if k != "output"} | {"modules": modules, "findings": len(found)}
        record["diagnostics"] += found
    else:
        record["lint"] = {"skipped": "no TauCeti module changed since the base"}
    record["status"] = "failed" if record["diagnostics"] else "passed"
    record["seconds"] = round(time.monotonic() - started, 1)
    return record


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
        build = _sandboxed([*lake, "build", "--iofail"], worktree, env, None, logdir / "verify-build.log", timeout)
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
    logdir = root / repair_tools.TOOLS_DIR / "audit"
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

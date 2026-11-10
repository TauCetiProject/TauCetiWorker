"""Advisory checks for a repair agent; the worker still owns full verification.

The existing native lint driver checks the reported linters on the finding modules
and modules edited since the failing candidate. Duplicate findings use the pinned
ownership-only driver. These scoped checks never authorize submission.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import time
from pathlib import Path

from tauceti_queue import repair_audit as audit
from tauceti_queue import repair_tools

FILE = "check-findings"
SETTINGS = "findings.json"
NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_']*(?:\.[A-Za-z_][A-Za-z0-9_']*)*")


def write(directory: Path, *, candidate: str, findings: dict) -> Path:
    from tauceti_queue import repair

    lint = (findings.get("lint") or {}).get("findings") or []
    modules = {m.removesuffix(".lean").replace("/", ".") for m in findings.get("failed_modules") or []}
    for path in repair.finding_paths(findings):
        module = repair.path_to_module(path) or (path if path.startswith("TauCeti.") and NAME.fullmatch(path) else None)
        if module:
            modules.add(module)
    for row in [*lint, *(findings.get("compiler_errors") or [])]:
        module = row.get("module")
        if not module and row.get("path", "").endswith(".lean"):
            module = row["path"][:-5].replace("/", ".")
        if module:
            modules.add(module)
    for duplicate in findings.get("duplicate_declarations") or []:
        modules.update(duplicate.get("modules") or [])
    settings = {
        "candidate": candidate,
        "modules": sorted(modules),
        "linters": sorted({r["linter"] for r in lint if r.get("linter") not in (None, "lint", "environment_linter")}),
        "unchecked": bool(
            findings.get("other")
            or findings.get("import_collisions")
            or any(r.get("linter") in (None, "lint", "environment_linter") for r in lint)
        ),
        "duplicates": bool(findings.get("duplicate_declarations")),
    }
    directory.mkdir(parents=True, exist_ok=True)
    (directory / SETTINGS).write_text(json.dumps(settings) + "\n")
    script = directory / FILE
    project = str(Path(__file__).resolve().parent.parent)
    script.write_text(
        "#!/usr/bin/env python3\nimport sys\nfrom pathlib import Path\n"
        f"sys.path.insert(0, {project!r})\n"
        "from tauceti_queue.repair_targeted import run\n"
        "raise SystemExit(run(Path(__file__).resolve().parent.parent))\n"
    )
    script.chmod(0o755)
    return script


def edited_modules(root: Path, candidate: str) -> set[str]:
    """Include committed, staged, unstaged and new source edits in focused feedback."""
    changed = audit._run(
        [
            "git",
            "-C",
            root,
            "diff",
            "--no-ext-diff",
            "--no-textconv",
            "--no-renames",
            "--name-only",
            "-z",
            "--diff-filter=d",
            candidate,
            "--",
            "TauCeti/",
        ],
        timeout=120,
    )
    untracked = audit._run(
        [
            "git",
            "-C",
            root,
            "ls-files",
            "--others",
            "--exclude-standard",
            "-z",
            "--",
            "TauCeti/",
        ],
        timeout=120,
    )
    if changed.returncode or untracked.returncode:
        raise audit.Unavailable("could not identify the repair's source delta")
    return {
        path[:-5].replace("/", ".")
        for path in (changed.stdout + untracked.stdout).split("\0")
        if audit.MODULE_PATH.fullmatch(path)
    }


def run(root: Path) -> int:
    settings = audit.read_settings(root)
    if not settings:
        print("targeted checks unavailable: no worker audit settings; return for worker verification", flush=True)
        return 2
    findings = json.loads((root / repair_tools.TOOLS_DIR / SETTINGS).read_text())
    # Include the actual repair delta, rather than all hundreds of PRs in the batch.
    modules = sorted(set(findings["modules"]) | edited_modules(root, findings["candidate"]))
    if not modules or any(not m.startswith("TauCeti.") or not NAME.fullmatch(m) for m in modules):
        print("targeted checks need located TauCeti modules; use the scoped build helper", flush=True)
        return 2
    linters = findings["linters"]
    if any(not NAME.fullmatch(linter) for linter in linters):
        print("targeted checks unavailable: invalid linter name", flush=True)
        return 2
    parent = Path(settings.get("logdir") or root / repair_tools.TOOLS_DIR / "audit")
    logdir = parent / ("targeted-" + str(time.time_ns()))
    logdir.mkdir(parents=True)
    record = {"advisory": True, "modules": modules, "linters": linters, "status": "failed"}
    started = time.monotonic()
    lock = root / repair_tools.TOOLS_DIR / repair_tools.LOCK_DIR
    repair_tools.take_lock(lock)
    try:
        env = dict(os.environ)
        lake = tuple(settings.get("lake") or ("lake",))
        build = audit._sandboxed([*lake, "build", "--iofail", *modules], root, env, None, logdir / "build.log", None)
        record["build"] = {k: v for k, v in build.items() if k != "output"}
        print(build["output"], end="", flush=True)
        if build["rc"]:
            return 1
        if linters or findings["duplicates"]:
            repo, ref = audit.tools_pin(root)
            checkout = audit.tools_checkout(Path(settings["tools_root"]), repo, ref)
            work = root / audit.WORK_DIR
            scripts = audit.materialize(checkout, root, work / "scripts")
            drivers, built = audit.build_drivers(root, scripts, env, ref, logdir / "drivers.log", None)
            if built:
                record["drivers"] = built
            if linters:
                scope, marker = work / "targeted-modules.txt", work / "targeted-marker.txt"
                scope.write_text("".join(m + "\n" for m in modules))
                nonce = "repair-targeted-" + os.urandom(16).hex()
                marker.write_text(nonce + "\n")
                lint = audit._sandboxed(
                    [
                        *lake,
                        "env",
                        drivers / "lint-env-driver",
                        "--only-listed",
                        "repair-targeted",
                        marker,
                        scope,
                        *linters,
                    ],
                    root,
                    env,
                    None,
                    logdir / "lint.log",
                    None,
                )
                record["lint"] = {k: v for k, v in lint.items() if k != "output"}
                print(lint["output"], end="", flush=True)
                if nonce not in lint["output"].splitlines():
                    record.update(status="unavailable", reason="linter did not complete its report")
                    print("targeted linter did not complete its report; return for worker verification", flush=True)
                    return 1
                if lint["rc"]:
                    return 1
            if findings["duplicates"]:
                duplicates = audit._sandboxed(
                    [*lake, "env", "timeout", "3000s", drivers / "audit-driver", "--duplicates-only"],
                    root,
                    env,
                    None,
                    logdir / "duplicates.log",
                    None,
                )
                record["duplicates"] = {k: v for k, v in duplicates.items() if k != "output"}
                print(duplicates["output"], end="", flush=True)
                if duplicates["rc"]:
                    return 1
        if findings.get("unchecked"):
            record["status"] = "partial"
            print("scoped compilation passed; some findings need worker verification", flush=True)
            return 2
        record["status"] = "passed"
        print("targeted checks passed; return for the worker's full build, audits and lint", flush=True)
        return 0
    except (audit.Unavailable, OSError, ValueError) as error:
        record.update(status="unavailable", reason=str(error))
        print(f"targeted checks unavailable: {error}; return for worker verification", flush=True)
        return 2
    finally:
        record["seconds"] = round(time.monotonic() - started, 1)
        try:
            (logdir / "result.json").write_text(json.dumps(record, indent=2) + "\n")
        finally:
            shutil.rmtree(lock, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(run(Path.cwd()))

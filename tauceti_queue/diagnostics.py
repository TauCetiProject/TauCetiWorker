"""Source and infrastructure findings of a completed candidate run.

The controller hands `CandidateRun.diagnostics` to the repair task unchanged and classifies the run
by them: dicts with `kind` (`compiler`, `audit`, `lint` or `infrastructure`), `module` and
`message` (`tauceti_queue/ports.py`). The source findings are read from the `sandboxed-build` job
log with the patterns bors-ng's `priv/failure_diagnostics.py` uses, which the staging CI contract
guarantees (every independently failing module is reported; audit and lint findings carry the
defining module path):

| log line | finding |
| --- | --- |
| `error: TauCeti/A/B.lean:12:3: error: ...` or `TauCeti/A/B.lean:12:3: error: ...` | `compiler` on `TauCeti.A.B` |
| `audit-driver: import TauCeti.X failed, environment already contains '...' from TauCeti.Y` | `audit` (duplicate declaration) on both modules |
| `duplicate-declarations: name is declared in both M and N` | `audit` on the project modules |
| `LINT-ENV-LOCATIONS {...}` (the environment linter's JSON findings) | `lint` on the defining path, `unknown` kept when the linter could not locate it |
| `Some required targets logged failures:` followed by `- TauCeti.A.B` lines | `compiler` on each target, only when no located finding exists |

Infrastructure findings come from the run and jobs rather than the log: a cancelled, timed out or
startup-failed run, a lost runner (the job log says so), and a finalizer that failed to publish the
`build` status. Logs are untrusted text: they are matched, never executed, and the read is bounded.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable

ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
STAMP = re.compile(r"^\d{4}-\d\d-\d\dT[0-9:.]+Z ")
PATH = r"TauCeti(?:/[A-Za-z0-9_\-]+)+\.lean"
MODULE = r"TauCeti(?:\.[A-Za-z0-9_]+)+"
SOURCE = re.compile(r"^(?:error: )?(" + PATH + r"):(\d+):(\d+):\s*(?:error:|error\b)")
LAKE = re.compile(r"^error: (" + PATH + r"):(\d+):(\d+):")
AUDIT = re.compile(
    r"^audit-driver: import ("
    + MODULE
    + r") failed, environment already contains '([^'\r\n]{1,512})' from ("
    + MODULE
    + r")$"
)
ANY_MODULE = r"[A-Za-z_][A-Za-z0-9_']*(?:\.[A-Za-z_][A-Za-z0-9_']*)*"
DUPLICATE = re.compile(
    r"^duplicate-declarations: (\S{1,1024}) is declared in both (" + ANY_MODULE + r") and (" + ANY_MODULE + r")$"
)
ENV_LOCATIONS = "LINT-ENV-LOCATIONS "
ENV_PATH = re.compile(r"TauCeti(?:/[A-Za-z_][A-Za-z0-9_']*)+\.lean")
FAILED_TARGETS_HEADER = "Some required targets logged failures:"
FAILED_TARGET = re.compile(r"- (TauCeti(?:\.[A-Za-z_][A-Za-z0-9_']*)+)")
RUNNER_LOST = re.compile(
    r"The runner has received a shutdown signal|lost communication with the server|"
    r"The hosted runner .* lost communication|runner has stopped responding|No space left on device",
    re.IGNORECASE,
)
MAX_MESSAGE = 512
MAX_FINDINGS = 2000


def module_of(path: str) -> str:
    return path.removesuffix(".lean").replace("/", ".")


def path_of(module: str) -> str:
    return module.replace(".", "/") + ".lean"


def _finding(kind: str, module: str, message: str, **extra) -> dict:
    return {"kind": kind, "module": module, "message": message[:MAX_MESSAGE], **extra}


def parse_log(lines: Iterable[str]) -> list[dict]:
    """Source findings in a job log, deduplicated, in the order first seen."""
    located: dict[tuple, dict] = {}
    fallback: dict[tuple, dict] = {}
    in_targets = False
    runner_lost: dict | None = None
    for raw in lines:
        if len(located) >= MAX_FINDINGS:
            break
        line = STAMP.sub("", ANSI.sub("", raw.rstrip("\r\n").lstrip("﻿")))
        if line.startswith(ENV_LOCATIONS):
            for finding in environment_findings(line[len(ENV_LOCATIONS) :]):
                located.setdefault(("lint", finding["module"], finding.get("declaration")), finding)
            continue
        if (audit := AUDIT.fullmatch(line)) and all(len(m) <= 512 for m in audit.groups()):
            for module in (audit.group(1), audit.group(3)):
                located.setdefault(
                    ("audit", module, audit.group(2)),
                    _finding("audit", module, line, path=path_of(module), declaration=audit.group(2)),
                )
            continue
        if (dup := DUPLICATE.fullmatch(line)) and any(m.startswith("TauCeti.") for m in dup.groups()[1:]):
            for module in dup.groups()[1:]:
                if module.startswith("TauCeti.") and len(module) <= 508:
                    located.setdefault(
                        ("audit", module, dup.group(1)),
                        _finding("audit", module, line, path=path_of(module), declaration=dup.group(1)),
                    )
            continue
        source = SOURCE.match(line) or LAKE.match(line)
        if source and len(source.group(1)) <= 512:
            path = source.group(1)
            key = ("compiler", path, source.group(2), source.group(3))
            located.setdefault(
                key,
                _finding(
                    "compiler",
                    module_of(path),
                    line,
                    path=path,
                    line_number=int(source.group(2)),
                    column=int(source.group(3)),
                ),
            )
            continue
        if line == FAILED_TARGETS_HEADER:
            in_targets = True
            continue
        if in_targets:
            target = FAILED_TARGET.fullmatch(line)
            if target:
                fallback.setdefault(
                    ("compiler", target.group(1)),
                    _finding("compiler", target.group(1), line, path=path_of(target.group(1)), located=False),
                )
                continue
            in_targets = False
        if runner_lost is None and RUNNER_LOST.search(line):
            runner_lost = _finding("infrastructure", "", line, reason="runner_lost")
    findings = list(located.values()) or list(fallback.values())
    if runner_lost is not None:
        findings.append(runner_lost)
    return findings


def environment_findings(payload: str) -> list[dict]:
    """The environment linter's located findings; malformed payloads yield nothing."""
    try:
        data = json.loads(payload)
    except ValueError:
        return []
    if not isinstance(data, dict) or data.get("version") != 1 or not isinstance(data.get("findings"), list):
        return []
    findings = []
    for entry in data["findings"]:
        if not isinstance(entry, dict):
            continue
        linter, declaration = entry.get("linter"), entry.get("declaration")
        if not isinstance(linter, str) or not isinstance(declaration, str):
            continue
        path = entry.get("path")
        if isinstance(path, str) and ENV_PATH.fullmatch(path) and len(path) <= 512:
            message = f"{linter}: {declaration} ({path}"
            if isinstance(entry.get("line"), int):
                message += f":{entry['line']}:{entry.get('column', 0)}"
            finding = _finding(
                "lint", module_of(path), message + ")", path=path, linter=linter, declaration=declaration
            )
            if isinstance(entry.get("line"), int):
                finding["line_number"] = entry["line"]
        elif isinstance(entry.get("unknown"), str):
            finding = _finding(
                "lint",
                "",
                f"{linter}: {declaration} (location unknown: {entry['unknown']})",
                linter=linter,
                declaration=declaration,
                unknown=entry["unknown"],
            )
        else:
            continue
        findings.append(finding)
    return findings


def infrastructure_findings(run: dict, jobs: Iterable[dict], *, build_status_posted: bool | None = None) -> list[dict]:
    """Findings about the run itself: cancellation, timeouts, startup failures, a failed finalizer."""
    findings: list[dict] = []
    conclusion = run.get("conclusion")
    if conclusion in ("cancelled", "timed_out", "startup_failure", "action_required", "stale"):
        findings.append(
            _finding("infrastructure", "", f"workflow run {run.get('id')} concluded {conclusion}", reason=conclusion)
        )
    for job in jobs:
        name = str(job.get("name") or "")
        if job.get("conclusion") in ("cancelled", "timed_out"):
            findings.append(
                _finding(
                    "infrastructure",
                    "",
                    f"job {name} concluded {job['conclusion']}",
                    reason=job["conclusion"],
                    job=name,
                )
            )
        elif job.get("conclusion") == "failure" and name == "finalize-merge-group-build":
            findings.append(
                _finding(
                    "infrastructure",
                    "",
                    f"job {name} failed: the build status may not have been published",
                    reason="finalizer_failed",
                    job=name,
                )
            )
    if build_status_posted is False and conclusion == "failure" and not findings:
        findings.append(
            _finding(
                "infrastructure", "", "the run completed without publishing the build status", reason="missing_status"
            )
        )
    return findings

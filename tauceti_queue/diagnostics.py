"""Source and infrastructure findings of a completed candidate run.

The controller hands `CandidateRun.diagnostics` to the repair task unchanged and classifies the run
by them: dicts with `kind` (`compiler`, `audit`, `lint`, `policy` or `infrastructure`), `module` and
`message` (`tauceti_queue/ports.py`). The source findings are read from the `sandboxed-build` job
log with the patterns bors-ng's `priv/failure_diagnostics.py` uses, which the staging CI contract
guarantees (every independently failing module is reported; audit and lint findings carry the
defining module path):

| log line | finding |
| --- | --- |
| `error: TauCeti/A/B.lean:12:3: error: ...` or `TauCeti/A/B.lean:12:3: error: ...` | `compiler` on `TauCeti.A.B` |
| `audit-driver: import TauCeti.X failed, environment already contains '...' from M` | `audit` (duplicate declaration) on `TauCeti.X`, and on `M` when it is a project module |
| `duplicate-declarations: name is declared in both M and N` | `audit` on the project modules |
| `LINT-ENV-LOCATIONS {...}` (the environment linter's JSON findings) | `lint` on the defining path, `unknown` kept when the linter could not locate it |
| `TauCeti/A/B.lean:12:0: error: dot-notation namespace lint: D puts ...` | `lint` (`dot-notation`) on `TauCeti.A.B`, declaration `D` |
| `error: TauCeti/A/B.lean:12: ...` (the style lint's text linters, which give no column) | `lint` (`style`) on `TauCeti.A.B` |
| `Some required targets logged failures:` followed by `- TauCeti.A.B` lines | `compiler` on each target, only when no located finding exists |
| `TauCeti/A.lean: missing project import ...` and other located import-header findings | `compiler` on the importing module |
| `MEMBER-POLICY-REJECTED {...}` (the source member review-size policy's JSON record of one member) | `policy` naming that member by its accepted head and source identity |

Infrastructure findings come from the run and jobs rather than the log: a cancelled, timed out or
startup-failed run, a lost runner (the job log says so), a Lean process the watchdog reports killed
by signal 9 before its deadline (`reason` `lean_killed` on that module, whose failed-target entry is
then dropped; located findings elsewhere still make the run a source failure), and a failed
finalizer, which may not have published the `build` status (it also exits nonzero after posting a
failed one, so the controller ignores this finding once the run's own `build` status is bound). Logs
are untrusted text: they are matched, never executed, and the read is bounded.
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
# The lint scripts print located findings in the compiler's shape; these are lint findings.
DOT_NOTATION = re.compile(r"^(" + PATH + r"):(\d+):\d+: error: dot-notation namespace lint: (\S{1,1024}) ")
STYLE = re.compile(r"^error: (?:\./)?(" + PATH + r"):(\d+): (.+)$")
# Their `linter` names; the environment lint driver cannot check these.
SOURCE_LINTERS = ("dot-notation", "style")
# The style lint's header check (TauCetiTools `HeaderStyle.lean`) prints `path: message`. Other
# scripts use that shape too (`check-imports.py`), so only the style lint's own output is read for it.
HEADER_STYLE = re.compile(r"^(" + PATH + r"): (.+)$")
ANY_MODULE = r"[A-Za-z_][A-Za-z0-9_']*(?:\.[A-Za-z_][A-Za-z0-9_']*)*"
# The second module is the earlier definition: a project module, or a dependency's (a Mathlib bump can
# add a declaration a project module already defines). Findings name only the project modules.
AUDIT = re.compile(
    r"^audit-driver: import ("
    + MODULE
    + r") failed, environment already contains '([^'\r\n]{1,512})' from ("
    + ANY_MODULE
    + r")$"
)
DUPLICATE = re.compile(
    r"^duplicate-declarations: (\S{1,1024}) is declared in both (" + ANY_MODULE + r") and (" + ANY_MODULE + r")$"
)
ENV_LOCATIONS = "LINT-ENV-LOCATIONS "
# One line per member the source member review-size policy rejects (TauCetiTools
# `ci_candidate_workflow.member_size_policy`): `source_pr` (the PR number), `source` (the repository
# identity record), `source_forge`, `head` (the accepted head), `reasons`, the measured sizes and,
# from a TauCetiTools that measures the batch chain, `measured`, `batch_commit` and `parent`.
MEMBER_POLICY_REJECTED = "MEMBER-POLICY-REJECTED "
# A record's `measured`: `batch-commit` (the member's own commit in the batch chain against its
# `parent`, which a repair in the batch can change) or `accepted-head` (against its merge base).
MEASURED_BATCH_COMMIT = "batch-commit"
MEASURED_ACCEPTED_HEAD = "accepted-head"
SHA = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
ENV_PATH = re.compile(r"TauCeti(?:/[A-Za-z_][A-Za-z0-9_']*)+\.lean")
IMPORT_HEADER = re.compile(
    r"^(TauCeti(?:/[A-Za-z_][A-Za-z0-9_']*)*\.lean): "
    r"(?:missing project import |Lean import header does not parse|Lake import header does not parse|"
    r"does not opt into the module system|`meta import |direct metaprogramming import |ambiguous project imports:)"
)
FAILED_TARGETS_HEADER = "Some required targets logged failures:"
FAILED_TARGET = re.compile(r"- (TauCeti(?:\.[A-Za-z_][A-Za-z0-9_']*)+)")
# TauCetiTools' Lean watchdog reports a Lean process SIGKILLed before its wall-clock deadline, almost
# always by the kernel's out-of-memory killer: a fault of the runner, not of the source. Its path is
# absolute (the checkout's), so the project path is its last `TauCeti/...` suffix.
LEAN_KILLED = re.compile(
    r"^error: Lean process for (?:\S*/)?(" + PATH + r") was killed by signal 9 after \d+s, likely out of memory$"
)
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
    killed: dict[str, dict] = {}
    for raw in lines:
        if len(located) >= MAX_FINDINGS:
            break
        line = STAMP.sub("", ANSI.sub("", raw.rstrip("\r\n").lstrip("﻿")))
        if line.startswith(MEMBER_POLICY_REJECTED):
            finding = member_policy_finding(line[len(MEMBER_POLICY_REJECTED) :])
            if finding is not None:
                located.setdefault(("policy", finding["member_head"], finding["source_pr"]), finding)
            continue
        if line.startswith(ENV_LOCATIONS):
            for finding in environment_findings(line[len(ENV_LOCATIONS) :]):
                located.setdefault(("lint", finding["module"], finding.get("declaration")), finding)
            continue
        if (header := IMPORT_HEADER.match(line)) and len(header.group(1)) <= 512:
            path = header.group(1)
            located.setdefault(
                ("compiler", path, line[:MAX_MESSAGE]),
                _finding("compiler", module_of(path), line, path=path, reason="import_header"),
            )
            continue
        if (audit := AUDIT.fullmatch(line)) and all(len(m) <= 512 for m in audit.groups()):
            for module in (audit.group(1), audit.group(3)):
                if not module.startswith("TauCeti."):
                    continue
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
        if (dot := DOT_NOTATION.match(line)) and len(dot.group(1)) <= 512:
            path = dot.group(1)
            located.setdefault(
                ("lint", path, dot.group(3)),
                _finding(
                    "lint",
                    module_of(path),
                    line,
                    path=path,
                    linter="dot-notation",
                    declaration=dot.group(3),
                    line_number=int(dot.group(2)),
                ),
            )
            continue
        if (style := STYLE.match(line)) and len(style.group(1)) <= 512:
            path = style.group(1)
            located.setdefault(
                ("lint", path, style.group(2), style.group(3)[:MAX_MESSAGE]),
                _finding(
                    "lint",
                    module_of(path),
                    line,
                    path=path,
                    linter="style",
                    declaration=style.group(3),
                    line_number=int(style.group(2)),
                ),
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
        if (oom := LEAN_KILLED.match(line)) and len(oom.group(1)) <= 512:
            module = module_of(oom.group(1))
            killed.setdefault(module, _finding("infrastructure", module, line, path=oom.group(1), reason="lean_killed"))
            continue
        if line.startswith("LINT-ENV: FAIL"):
            fallback.setdefault(("lint", line[:MAX_MESSAGE]), _finding("lint", "", line))
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
    # Lake lists a killed module among its failed targets too; that entry is no source finding.
    findings = list(located.values()) or [f for f in fallback.values() if f["module"] not in killed]
    findings.extend(killed.values())
    if runner_lost is not None:
        findings.append(runner_lost)
    return findings


def header_style_findings(lines: Iterable[str]) -> list[dict]:
    """The style lint's located copyright-header findings, from that script's output only."""
    findings = []
    for line in lines:
        if (header := HEADER_STYLE.match(line.rstrip("\r\n"))) and len(header.group(1)) <= 512:
            path = header.group(1)
            findings.append(
                _finding("lint", module_of(path), line, path=path, linter="style", declaration=header.group(2))
            )
    return findings


def member_policy_finding(payload: str) -> dict | None:
    """A `policy` finding naming the member a review-size rejection record describes, or None when the
    record is malformed: the queue then attributes the failure as it would without the line."""
    try:
        data = json.loads(payload[: 16 * 1024])
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    number, head = data.get("source_pr"), data.get("head")
    if type(number) is not int or number <= 0 or not isinstance(head, str) or not SHA.fullmatch(head):
        return None
    reasons = [str(r)[:200] for r in data.get("reasons") or [] if isinstance(r, str)][:8]
    sizes = {k: data[k] for k in ("additions", "deletions", "files") if type(data.get(k)) is int}
    forge = data.get("source_forge") if isinstance(data.get("source_forge"), str) else ""
    # Where the policy measured the member: its own commit in the batch chain against that commit's
    # parent (repairable in the batch), or its accepted head against its merge base (a record without
    # the field comes from a TauCetiTools that measured only that, which no repair can change).
    chained = {
        k: data[k] for k in ("batch_commit", "parent") if isinstance(data.get(k), str) and SHA.fullmatch(data[k])
    }
    repairable = data.get("measured") == MEASURED_BATCH_COMMIT and len(chained) == 2
    measured = MEASURED_BATCH_COMMIT if repairable else MEASURED_ACCEPTED_HEAD
    sizes_text = ", ".join(f"{k} {v}" for k, v in sizes.items())
    where = f"its batch commit {chained['batch_commit'][:12]}" if repairable else head[:12]
    message = f"member review-size policy rejected #{number} at {where}: " + ("; ".join(reasons) or "rejected")
    finding = _finding(
        "policy",
        "",
        message + (f" ({sizes_text})" if sizes_text else ""),
        source_pr=number,
        member_head=head,
        source_forge=forge[:512],
        reasons=reasons,
        measured=measured,
        **(chained if repairable else {}),
        **sizes,
    )
    if isinstance(data.get("source"), dict):
        finding["source"] = data["source"]
    return finding


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
        # A stub caller of a reusable pr-build prefixes job names with `<caller job> / `.
        elif job.get("conclusion") == "failure" and name.rsplit(" / ", 1)[-1] == "finalize-merge-group-build":
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

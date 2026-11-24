"""Roadmap reports: how a worker says that a roadmap statement is wrong.

A worker never writes to the roadmap repository. When a roadmap statement or `Suggested.lean` pin is
wrong, it proves the corrected statement and leaves a report in the body of its own pull request: a
visible `### Roadmap report` section and one marker line per error,

    <!--tauceti-roadmap-report:v1 {"roadmap":"...","file":"README.md","roadmap_sha":"...", ...}-->

which the job board harvests from the library repository (docs/roadmap-reports.md).

Stdlib only and free of package imports: `scripts/roadmap-report-check`, which agents and
gh-safe-pr-create run inside the sandbox, loads this file on its own.
"""

from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path

PREFIX = "<!--tauceti-roadmap-report:v1 "
HEADING = "### Roadmap report"
MAX_MARKERS = 5
MAX_ANCHOR = 300
MAX_NAMES = 10
FILES = ("README.md", "Suggested.lean")
KINDS = (
    "false-statement",
    "missing-hypothesis",
    "unprovable-pin",
    "wrong-definition",
    "circular-dependency",
    "stale-name",
)
EFFECTS = ("blocked", "worked-around")

_SEGMENT = re.compile(r"[A-Za-z][A-Za-z0-9]*")
_SHA = re.compile(r"[0-9a-f]{40}")
_NAME = re.compile(r"[^\s`]{1,200}")


def normalise(text: str) -> str:
    """Runs of whitespace compared as one space."""
    return " ".join(text.split())


@dataclass(frozen=True)
class Report:
    roadmap: str
    file: str
    roadmap_sha: str
    anchor: str
    kind: str
    effect: str
    refutes: tuple = ()
    replaces: tuple = ()


def problems(value) -> list[str]:
    """Why a decoded marker is not a valid version 1 report; empty when it is. Unknown fields are allowed."""
    if not isinstance(value, dict):
        return ["the marker is not a JSON object"]
    found = []
    roadmap = value.get("roadmap")
    if not isinstance(roadmap, str) or not all(_SEGMENT.fullmatch(part) for part in roadmap.split("/")):
        found.append("`roadmap` must be the roadmap's folder, `/`-separated segments matching [A-Za-z][A-Za-z0-9]*")
    if value.get("file") not in FILES:
        found.append("`file` must be `README.md` or `Suggested.lean`")
    if not isinstance(value.get("roadmap_sha"), str) or not _SHA.fullmatch(value["roadmap_sha"]):
        found.append("`roadmap_sha` must be the 40-hex roadmap commit you read")
    anchor = value.get("anchor")
    if not isinstance(anchor, str) or not normalise(anchor):
        found.append("`anchor` must be a non-empty string")
    elif len(anchor) > MAX_ANCHOR:
        found.append(f"`anchor` is longer than {MAX_ANCHOR} characters")
    if value.get("kind") not in KINDS:
        found.append("`kind` must be one of " + ", ".join(KINDS))
    if value.get("effect") not in EFFECTS:
        found.append("`effect` must be `blocked` or `worked-around`")
    for key in ("refutes", "replaces"):
        names = value.get(key, [])
        if not isinstance(names, list) or len(names) > MAX_NAMES:
            found.append(f"`{key}` must be a list of at most {MAX_NAMES} declaration names")
        elif not all(isinstance(name, str) and _NAME.fullmatch(name) for name in names):
            found.append(f"`{key}` must hold fully qualified declaration names")
    return found


def read(body: str) -> tuple[list[Report], list[str]]:
    """Every marker in a body: the valid reports and a problem per invalid marker.

    A marker runs from the literal prefix to the next `-->`. Only the first MAX_MARKERS count; an
    invalid marker never invalidates the others."""
    reports, errors = [], []
    start, index = 0, 0
    body = body or ""
    while (begin := body.find(PREFIX, start)) >= 0:
        index += 1
        end = body.find("-->", begin + len(PREFIX))
        if end < 0:
            errors.append(f"report marker {index}: no closing `-->`")
            break
        start = end + 3
        if index > MAX_MARKERS:
            errors.append(f"report marker {index}: a body carries at most {MAX_MARKERS} report markers")
            continue
        try:
            value = json.loads(body[begin + len(PREFIX) : end])
        except ValueError:
            errors.append(f"report marker {index}: not one JSON object")
            continue
        found = problems(value)
        if found:
            errors.extend(f"report marker {index}: {problem}" for problem in found)
            continue
        reports.append(
            Report(
                roadmap=value["roadmap"],
                file=value["file"],
                roadmap_sha=value["roadmap_sha"],
                anchor=value["anchor"],
                kind=value["kind"],
                effect=value["effect"],
                refutes=tuple(value.get("refutes", ())),
                replaces=tuple(value.get("replaces", ())),
            )
        )
    return reports, errors


def anchor_problem(report: Report, roadmaps: Path) -> str | None:
    """Why `report`'s anchor is not in its file, read from `roadmaps`, the roadmap repository's
    active directory checked out at `report.roadmap_sha`; None when it is there."""
    path = Path(roadmaps, *report.roadmap.split("/"), report.file)
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return f"`{report.roadmap}/{report.file}` is not in the roadmap checkout"
    if normalise(report.anchor) not in normalise(text):
        return f"the anchor does not occur in `{report.roadmap}/{report.file}`"
    return None


def check(body: str, roadmaps: Path | None = None) -> list[str]:
    """Everything wrong with the reports in a pull request body, for the agent that wrote it.
    With `roadmaps` (the active directory of the checkout at the reports' commit) anchors are checked too."""
    reports, errors = read(body)
    if (reports or errors) and not any(line.strip() == HEADING for line in (body or "").splitlines()):
        errors.append(f"a body with a report marker needs a `{HEADING}` section saying what is wrong")
    if roadmaps is not None:
        for report in reports:
            if problem := anchor_problem(report, roadmaps):
                errors.append(problem)
    return errors


def main(argv: list[str]) -> int:
    """`roadmap-report-check [--roadmaps DIR] [FILE]`: list what is wrong with the reports in a body
    (FILE, or standard input). Exit 0 when there is nothing to fix, 1 otherwise."""
    roadmaps = None
    if argv[:1] == ["--roadmaps"] and len(argv) >= 2:
        roadmaps, argv = Path(argv[1]), argv[2:]
    if len(argv) > 1 or argv[:1] in (["-h"], ["--help"]):
        print("usage: roadmap-report-check [--roadmaps DIR] [FILE]", file=sys.stderr)
        return 2
    try:
        body = Path(argv[0]).read_text(encoding="utf-8") if argv else sys.stdin.read()
    except (OSError, UnicodeDecodeError) as error:
        print(f"roadmap-report-check: cannot read {argv[0]}: {error}", file=sys.stderr)
        return 2
    errors = check(body, roadmaps)
    for error in errors:
        print(f"roadmap-report-check: {error}", file=sys.stderr)
    if not errors:
        count = len(read(body)[0])
        print(f"roadmap-report-check: {count} valid report marker(s)" if count else "roadmap-report-check: no report")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

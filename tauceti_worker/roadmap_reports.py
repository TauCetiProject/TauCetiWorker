"""Roadmap reports: how a worker says that a roadmap statement is wrong.

A worker never writes to the roadmap repository. When a roadmap statement or `Suggested.lean` pin is
wrong, it proves the corrected statement and leaves a report in the body of its own pull request: a
visible `### Roadmap report` section and one marker line per error,

    <!--tauceti-roadmap-report:v1 {"roadmap":"...","file":"README.md","roadmap_sha":"...", ...}-->

which the job board harvests from the library repository (docs/roadmap-reports.md). The board keeps a
register of the reports it knows; `fetch_register` and `known_block` put it in the authoring prompt.

Stdlib only and free of package imports: `scripts/roadmap-report-check`, which agents and
gh-safe-pr-create run inside the sandbox, loads this file on its own.
"""

from __future__ import annotations

import http.client
import json
import re
import sys
import urllib.error
import urllib.request
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
# A declaration name: non-empty parts joined by single dots, none holding whitespace, `, <, > or @.
_NAME = re.compile(r"[^.\s`<>@]+(?:\.[^.\s`<>@]+)*")
MAX_NAME = 200


def declaration_name(value) -> bool:
    """Whether `value` is a declaration name as `refutes` and `replaces` hold them (at most 200 characters)."""
    return isinstance(value, str) and len(value) <= MAX_NAME and _NAME.fullmatch(value) is not None


# The board's register of known reports (docs/roadmap-reports.md). Blank turns the read off.
REGISTER_URL = "https://raw.githubusercontent.com/TauCetiProject/TauCetiRoadmapJobs/main/reports/register.json"
REGISTER_LIMIT = 1 << 20  # bytes read at most; a larger register is ignored
REGISTER_STATES = ("open", "confirmed", "rejected", "fixed")
# What the authoring prompt shows of the register, at most.
MAX_KNOWN = 20
MAX_KNOWN_CHARS = 6000


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


@dataclass(frozen=True)
class Known:
    """One entry of the board's register, from validated fields only."""

    roadmap: str
    file: str
    anchor: str
    kind: str
    state: str
    refutes: tuple = ()
    replaces: tuple = ()

    def line(self) -> str:
        """One prompt line. The anchor is JSON-quoted on one line, so it cannot leave its quotes."""
        names = "".join(
            f"; {label}: " + ", ".join(f"`{name}`" for name in names)
            for label, names in (("corrected by", self.replaces), ("refuted by", self.refutes))
            if names
        )
        quote = json.dumps(normalise(self.anchor), ensure_ascii=False)
        return f"- `{self.roadmap}/{self.file}` ({self.kind}, {self.state}): {quote}{names}"


def fetch_register(url: str, log, *, timeout: float = 10) -> dict | None:
    """The board's register at `url`, or None: a blank url, a 404 (the board has none yet) and any
    other failure, logged once, all leave the round as it would be without the register."""
    if not url:
        return None
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "TauCetiWorker/1.0"})
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = response.read(REGISTER_LIMIT + 1)
    except urllib.error.HTTPError as error:
        error.close()
        if error.code != 404:
            log(f"roadmap report register: HTTP {error.code}; proceeding without it")
        return None
    # Any failure while reading the body too: a truncated chunked response raises IncompleteRead,
    # an HTTPException; a timeout mid-read is an OSError.
    except (urllib.error.URLError, http.client.HTTPException, OSError, ValueError) as error:
        log(f"roadmap report register: unreadable ({type(error).__name__}); proceeding without it")
        return None
    if len(data) > REGISTER_LIMIT:
        log(f"roadmap report register: larger than {REGISTER_LIMIT} bytes; proceeding without it")
        return None
    try:
        value = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        value = None
    if not isinstance(value, dict) or value.get("version") != 1 or not isinstance(value.get("roadmaps"), dict):
        log("roadmap report register: not a version 1 register; proceeding without it")
        return None
    return value


def known_reports(register: dict | None) -> list[Known]:
    """The register's well-formed entries, sorted; entries with unknown states or bad fields are skipped."""
    found = []
    for roadmap, entries in ((register or {}).get("roadmaps") or {}).items():
        if not isinstance(roadmap, str) or not all(_SEGMENT.fullmatch(part) for part in roadmap.split("/")):
            continue
        for entry in entries if isinstance(entries, list) else ():
            if not isinstance(entry, dict):
                continue
            anchor = entry.get("anchor")
            if (
                entry.get("file") not in FILES
                or entry.get("kind") not in KINDS
                or entry.get("state") not in REGISTER_STATES
                or not isinstance(anchor, str)
                or not normalise(anchor)
                or len(anchor) > MAX_ANCHOR
            ):
                continue
            names = {
                key: tuple(
                    name
                    for name in (entry.get(key) if isinstance(entry.get(key), list) else ())[:MAX_NAMES]
                    if declaration_name(name)
                )
                for key in ("refutes", "replaces")
            }
            found.append(Known(roadmap, entry["file"], anchor, entry["kind"], entry["state"], **names))
    return sorted(found, key=lambda k: (k.roadmap, k.file, normalise(k.anchor).casefold()))


def known_block(known: list[Known], roadmap: str | None) -> str:
    """The `__REPORTED__` text for a round: the register's live entries for `roadmap` and the roadmaps
    nested in it (every roadmap when None), bounded; "none" when there are none."""
    chosen = [
        k
        for k in known
        if k.state != "fixed" and (roadmap is None or k.roadmap == roadmap or k.roadmap.startswith(roadmap + "/"))
    ]
    lines, size = [], 0
    for k in chosen[:MAX_KNOWN]:
        line = k.line()
        if size + len(line) > MAX_KNOWN_CHARS:
            break
        lines.append(line)
        size += len(line) + 1
    if len(lines) < len(chosen):
        lines.append(f"- ({len(chosen) - len(lines)} more not shown)")
    return "\n".join(lines) or "none"


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
        elif not all(declaration_name(name) for name in names):
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
        except (ValueError, RecursionError):  # deep nesting must be an invalid marker, never a crash
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

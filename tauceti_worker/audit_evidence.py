"""One reading of an inspection attempt's evidence, shared by every caller.

Every finished, failed or interrupted attempt is exactly one outcome, in this precedence:

- FINDING: a binding judgment names a concern. Binding judgments are the deciding reviewer's
  API response (the escalation reviewer's once it was asked, else the triage reviewer's
  pass), the attempts this one continued, and the ledger's current-protocol verdicts for the
  same input or any of its commits. It binds whatever else the attempt did.
- UNREADABLE: no concern was found, but some evidence cannot be read, so the absence of a
  concern is not established. It holds the range for a human, exactly like a finding.
- ESCALATE: the triage reviewer raised a concern, or gave an answer that cannot be read, and
  the escalation reviewer has not judged yet. It never binds by itself.
- PASS: the deciding request completed normally and its answer is a valid pass of the exact batch.
- NOT_JUDGED: everything else (malformed output, a refusal, an API failure or a request too
  large to judge). It never binds.

A concern is a judgment's nonempty findings, or a `finding` label even without findings.
Concerns are collected before any validation, so an invalid attempt can never discard one.

A commit reviewed in parts keeps each part's stages in its own directory, named by the attempt's
part plan (`PLANS`). Each part is read like a whole attempt, and the attempt is their union: any
part's concern or unreadable evidence binds the commit, any part's unescalated triage concern
escalates it, and it passes only if every planned part has a valid pass.
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from .config import Die

FINDING, UNREADABLE, PASS, NOT_JUDGED = "finding", "unreadable", "pass", "not-judged"
ESCALATE = "escalate"
TRIAGE, ESCALATION = "", "escalation-"  # The file-name prefix of each stage (security_audit).
PLANS = "parts.json"  # The part plan of a commit reviewed in parts (security_audit.PLANS).
PART = re.compile(r"part-\d{4}")

# Text that mentions a judgment key must contain a readable judgment.
KEYS = ("verdict", "findings")  # Either key makes a decoded object a judgment.
VALUE_START = re.compile(r'[{\["]')


def unique_pairs(pairs: list) -> dict:
    """An object that repeats a key could say two things; it is not readable as either."""
    keys = [key for key, _ in pairs]
    if len(set(keys)) != len(keys):
        raise ValueError("duplicate key")
    return dict(pairs)


DECODER = json.JSONDecoder(object_pairs_hook=unique_pairs)
# Response blocks that never hold an answer: reasoning.
ANSWERLESS = frozenset({"thinking", "redacted_thinking"})


class NotJudged(Die):
    """The attempt recorded no concern and no valid complete judgment. It never binds."""


class NeedsEscalation(NotJudged):
    """The triage reviewer raised a concern that the escalation reviewer has not yet judged.

    The range is retried with the escalation reviewer only, never with the triage reviewer."""


class Unreadable(Die):
    """The attempt's evidence cannot establish the absence of a concern. It holds for a human."""


@dataclass(frozen=True)
class Outcome:
    kind: str
    verdict: dict | None = None  # PASS or FINDING: the verdict bound to the exact batch.
    reason: str = ""  # NOT_JUDGED or UNREADABLE: why.


@dataclass
class Evidence:
    judgments: list = field(default_factory=list)  # Every judgment-shaped value, in recorded order.
    tools: list[str] = field(default_factory=list)  # Every response block that is neither answer nor reasoning.
    unreadable: list[str] = field(default_factory=list)  # Why some evidence could not be read.
    escalate: list[str] = field(default_factory=list)  # Triage concerns awaiting the escalation reviewer.

    def concerns(self) -> list[tuple[str, list[str]]]:
        return [(j.get("verdict"), c) for j in self.judgments if (c := concern(j))]


def concern(value) -> list[str]:
    """The concern a recorded judgment names, as strings; empty when it names none."""
    if not isinstance(value, dict):
        return []
    findings = value.get("findings")
    if isinstance(findings, list):
        texts = [f if isinstance(f, str) else json.dumps(f, sort_keys=True) for f in findings]
    elif findings in (None, "", {}):
        texts = []
    else:
        texts = [findings if isinstance(findings, str) else json.dumps(findings, sort_keys=True)]
    texts = [t for t in texts if t.strip()]
    if not texts and value.get("verdict") == "finding":
        texts = ["The reviewer returned a finding without an explanation."]
    return texts


def walk(value, found: list, loose: list) -> None:
    """Collect judgment-shaped objects anywhere in a decoded value, decoding JSON carried in strings.

    Every level of JSON-in-a-string is decoded: each is strictly shorter than its encoding."""
    if isinstance(value, dict):
        if any(key in value for key in KEYS):
            found.append(value)
        for item in value.values():
            walk(item, found, loose)
    elif isinstance(value, list):
        for item in value:
            walk(item, found, loose)
    elif isinstance(value, str):
        scan(value, found, loose)


def scan(text: str, found: list, loose: list) -> None:
    """Decode every JSON value in text, never pattern-match it.

    Keys are compared after decoding, so escapes cannot hide them. A judgment key that is not
    inside any decodable object (malformed JSON, or prose quoting the key) is `loose`: the text
    may hold a concern that cannot be read, whatever else it holds."""
    i = 0
    while match := VALUE_START.search(text, i):
        try:
            value, i = DECODER.raw_decode(text, match.start())
        except ValueError:
            i = match.start() + 1
            continue
        if match.group() == '"' and value in KEYS:
            loose.append(value)
        else:
            walk(value, found, loose)


def judgments_in(value, evidence: Evidence, where: str, *, answer: bool = False) -> None:
    """Record the judgments in one reviewer output: text, or an answer payload already decoded.

    An answer (a final message or structured output) must be a readable judgment; any output
    with a judgment key that cannot be read is unreadable evidence."""
    found, loose = [], []
    (scan if isinstance(value, str) else walk)(value, found, loose)
    evidence.judgments.extend(found)
    if loose:
        evidence.unreadable.append(f"{where}: reviewer output mentions a judgment that cannot be read")
    elif answer and not found:
        evidence.unreadable.append(f"{where}: reviewer answer is not a judgment")


def read_response(path: Path, evidence: Evidence) -> None:
    """The judgments in one retained API response."""
    if not path.exists():
        return
    try:
        response = json.loads(path.read_text(encoding="utf-8"))
        content = response["content"]
        if not isinstance(content, list) or any(not isinstance(b, dict) for b in content):
            raise ValueError("response content is not a list of blocks")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        evidence.unreadable.append(f"{path}: {exc}")
        return
    for number, block in enumerate(content):
        where = f"{path}:content[{number}]"
        if block.get("type") == "text":
            judgments_in(block.get("text", ""), evidence, where, answer=True)
        elif block.get("type") not in ANSWERLESS:
            evidence.tools.append(str(block.get("type")))


def planned_parts(attempt: Path) -> list[Path] | None:
    """The part directories a commit reviewed in parts needs, or None for a single-request attempt.

    Raises ValueError when the plan cannot be read, or when part directories exist without one."""
    path = attempt / PLANS
    if not path.exists():
        if any(attempt.glob("part-*")):
            raise ValueError("part directories without a part plan")
        return None
    plan = json.loads(path.read_text(encoding="utf-8"))
    names = [p["dir"] for p in plan["parts"]]
    if not names or len(set(names)) != len(names) or not all(isinstance(n, str) and PART.fullmatch(n) for n in names):
        raise ValueError("malformed part plan")
    if any(p.name not in names for p in attempt.glob("part-*")):
        raise ValueError("a part directory the plan does not name")
    return [attempt / name for name in names]


def stage_dirs(attempt: Path) -> list[Path]:
    """Every directory holding stages of this attempt: itself, and its planned parts."""
    try:
        return [attempt, *(planned_parts(attempt) or [])]
    except (OSError, ValueError, KeyError, TypeError):
        return [attempt, *sorted(attempt.glob("part-*"))]


def stage_escalated(here: Path) -> bool:
    return (here / f"{ESCALATION}request.json").exists()


def escalated(attempt: Path) -> bool:
    """Whether the escalation reviewer was asked, for the attempt or any of its parts."""
    return any(stage_escalated(here) for here in stage_dirs(attempt))


def answer_path(here: Path) -> Path:
    """The response that decides an attempt (or one part): the escalation reviewer's once asked."""
    return here / f"{ESCALATION if stage_escalated(here) else TRIAGE}response.json"


def collect(attempt: Path, evidence: Evidence | None = None, *, seen: set[Path] | None = None) -> Evidence:
    """Everything binding that the attempt and the attempts it continued recorded.

    Once the escalation reviewer was asked, its response alone decides; the triage response is
    kept as a record. Before that, a triage concern or unreadable triage answer is not binding:
    it is a reason to escalate (`Evidence.escalate`)."""
    evidence = Evidence() if evidence is None else evidence
    attempt = attempt.resolve()
    seen = set() if seen is None else seen
    if attempt in seen:
        evidence.unreadable.append(f"{attempt}: cyclic continuation lineage")
        return evidence
    seen.add(attempt)
    try:
        planned_parts(attempt)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        evidence.unreadable.append(f"{attempt / PLANS}: {exc}")
    for here in stage_dirs(attempt):
        collect_stages(here, evidence)
    previous = attempt / "previous-block.json"
    if previous.exists():
        try:
            parent = Path(json.loads(previous.read_text())["attempt"]).resolve()
            if parent.parent != attempt.parent or not parent.name.startswith("attempt-"):
                raise ValueError("continuation lineage is outside this audit state")
        except (OSError, ValueError, KeyError, TypeError) as exc:
            evidence.unreadable.append(f"{previous}: {exc}")
        else:
            collect(parent, evidence, seen=seen)
    return evidence


def collect_stages(here: Path, evidence: Evidence) -> None:
    """The binding evidence of one attempt directory, or of one part of a commit reviewed in parts."""
    if stage_escalated(here):
        read_response(answer_path(here), evidence)
        return
    triage = Evidence()
    read_response(answer_path(here), triage)
    reasons = [text for _, texts in triage.concerns() for text in texts] + triage.unreadable
    if reasons:
        evidence.escalate.extend(reasons)
    else:
        evidence.judgments.extend(triage.judgments)
        evidence.tools.extend(triage.tools)


def triage_judgment(attempt: Path) -> dict:
    """The triage reviewer's judgments (of every part), for the record of an escalated attempt."""
    triage = Evidence()
    for here in stage_dirs(attempt):
        read_response(here / f"{TRIAGE}response.json", triage)
    return {"judgments": triage.judgments, "unreadable": triage.unreadable}


def recorded(state: Path, batch: dict, evidence: Evidence | None = None) -> Evidence:
    """The ledger's current-protocol verdicts about this input or any of its commits.

    A concern recorded for a range binds every range containing any of its commits, until the
    cursor passes them: binding never depends on how ranges are later planned or split. A
    verdict or hold recorded under an earlier judgment protocol does not bind: its commits are
    inspected afresh, once, under the current protocol, whose own concerns then bind."""
    from .security_audit import PROTOCOL

    evidence = Evidence() if evidence is None else evidence
    digest, commits = batch["input_digest"], {c["sha"] for c in batch["commits"]}
    ledger = state / "ledger.jsonl"
    try:
        lines = ledger.read_text(encoding="utf-8", errors="replace").splitlines() if ledger.exists() else []
    except OSError as exc:
        evidence.unreadable.append(f"{ledger}: {exc}")
        return evidence
    for number, line in enumerate(lines, 1):
        try:
            row = json.loads(line) if line.strip() else {}
        except ValueError:
            row = None
        if not isinstance(row, dict):
            # The ledger is shared by every range: only a damaged row that names this input or
            # one of its commits can have held a verdict about it.
            if digest in line or any(sha in line for sha in commits):
                evidence.unreadable.append(f"{ledger}:{number}: unreadable row")
        elif row.get("protocol_version") != PROTOCOL:
            continue
        elif isinstance(row.get("verdict"), dict) and (
            row.get("batch") == digest or commits & (judged_commits(row["verdict"]) | judged_commits(row))
        ):
            evidence.judgments.append(row["verdict"])
        elif row.get("outcome") == UNREADABLE and (row.get("batch") == digest or commits & judged_commits(row)):
            # A hold on evidence that could not be read binds its commits exactly like a concern.
            evidence.unreadable.append(f"{ledger}:{number}: unreadable evidence holds these commits")
    return evidence


def judged_commits(verdict) -> set[str]:
    listed = verdict.get("commits") if isinstance(verdict, dict) else None
    return {sha for sha in listed if isinstance(sha, str)} if isinstance(listed, list) else set()


def finding(batch: dict, evidence: Evidence) -> dict | None:
    """The binding verdict for every recorded concern, or None."""
    concerns = evidence.concerns()
    if not concerns:
        return None
    findings = list(dict.fromkeys(text for _, texts in concerns for text in texts))
    # An explained legacy `incomplete` keeps its label; any other concern is a finding.
    label = "incomplete" if all(verdict == "incomplete" for verdict, _ in concerns) else "finding"
    return {
        "input_digest": batch["input_digest"],
        "commits": [c["sha"] for c in batch["commits"]],
        "verdict": label,
        "findings": findings,
    }


def completed_pass(attempt: Path, batch: dict, evidence: Evidence) -> dict:
    """The request's valid complete pass; raises with the reason when there is none."""
    from . import security_audit as sa

    if evidence.unreadable:
        raise Die("security-audit: retained evidence is unreadable")
    execution = json.loads((attempt / "execution.json").read_text())
    if execution.get("protocol_version") != sa.PROTOCOL:
        raise Die(f"security-audit: only a judgment-protocol-{sa.PROTOCOL} attempt can pass")
    if evidence.tools:
        raise Die(
            f"security-audit: reviewer answered with unexpected content: {', '.join(sorted(set(evidence.tools)))}"
        )
    parts = planned_parts(attempt)
    if parts is not None:
        plan = json.loads((attempt / PLANS).read_text())
        if (
            len(batch["commits"]) != 1
            or plan["input_digest"] != batch["input_digest"]
            or plan["commit"] != batch["commits"][0]["sha"]
            or not plan_covers(plan, batch["commits"][0])
        ):
            raise Die("security-audit: the part plan does not cover this exact commit")
    verdict = None
    for here in parts if parts is not None else [attempt]:
        response = json.loads(answer_path(here).read_text())
        if response.get("stop_reason") != "end_turn":
            raise Die(f"security-audit: reviewer stopped with {response.get('stop_reason')}")
        texts = [b["text"] for b in response["content"] if b.get("type") == "text"]
        if len(texts) != 1:
            raise Die("security-audit: reviewer answer is not exactly one judgment")
        verdict = sa.bind_verdict(json.loads(texts[0]), batch)
        if verdict["verdict"] != "pass":
            raise Die(f"security-audit: {verdict['verdict']} without a recorded concern")
    return verdict


HUNKS = re.compile(r"(\d+)-(\d+) of (\d+)")


def plan_covers(plan: dict, commit: dict) -> bool:
    """Whether the plan's parts hold every file of the commit exactly once: whole, or every hunk."""
    from .security_audit import hunks

    units: dict[str, list] = {}
    for p in plan["parts"]:
        for unit in p["units"]:
            units.setdefault(unit["path"], []).append(unit["hunks"])
    if set(units) != {f["path"] for f in commit["files"]}:
        return False
    for f in commit["files"]:
        found = units[f["path"]]
        if found == [None]:
            continue
        spans = [HUNKS.fullmatch(h) if isinstance(h, str) else None for h in found]
        if not all(spans):
            return False
        count = len(hunks(f["diff"])[1])
        covered = [k for m in spans for k in range(int(m[1]), int(m[2]) + 1)]
        if any(int(m[3]) != count for m in spans) or covered != list(range(1, count + 1)):
            return False
    return True


def classify(state: Path, attempt: Path, batch: dict, failure: BaseException | str | None = None) -> Outcome:
    """The attempt's outcome. `failure` is why its last launch did not complete normally."""
    evidence = recorded(state, batch, collect(attempt))
    verdict = finding(batch, evidence)
    if verdict is not None:
        return Outcome(FINDING, verdict=verdict)
    if evidence.unreadable:
        return Outcome(UNREADABLE, reason="security-audit: unreadable evidence: " + "; ".join(evidence.unreadable[:3]))
    if evidence.escalate:
        return Outcome(
            ESCALATE, reason="security-audit: triage concern awaiting escalation: " + "; ".join(evidence.escalate[:3])
        )
    if failure is not None:
        return Outcome(NOT_JUDGED, reason=failure_reason(failure))
    if not (attempt / "execution.json").exists():
        return Outcome(NOT_JUDGED, reason="security-audit: no reviewer was launched")
    try:
        return Outcome(PASS, verdict=completed_pass(attempt, batch, evidence))
    except (Die, OSError, ValueError, KeyError, TypeError, AttributeError, IndexError) as exc:
        return Outcome(NOT_JUDGED, reason=failure_reason(exc))


def failure_reason(failure: BaseException | str) -> str:
    if isinstance(failure, subprocess.TimeoutExpired):
        return "security-audit: reviewer timed out"
    text = str(failure) or type(failure).__name__
    return text if text.startswith("security-audit: ") else "security-audit: " + text


def holds(state: Path, attempt: Path) -> bool:
    """Whether a stopped attempt binds its range: a recorded concern, or evidence that cannot be read."""
    if not attempt.exists():
        return False  # Never created: nothing was recorded.
    try:
        batch = json.loads((attempt / "input.json").read_text())
        evidence = recorded(state, batch, collect(attempt))
    except (OSError, ValueError, KeyError, TypeError):
        return True
    return bool(evidence.concerns() or evidence.unreadable)


def engaged(attempt: Path) -> bool:
    """Whether the reviewer engaged with the input: it answered, or the request was too large.

    An attempt that is not a judgment is divided only then; a provider or setup failure, where
    no reviewer engaged, is retried on the same range."""
    for here in stage_dirs(attempt):
        for stage in (TRIAGE, ESCALATION):
            if (here / f"{stage}response.json").exists():
                return True
            try:
                if json.loads((here / f"{stage}reviewer-error.json").read_text()).get("too_large") is True:
                    return True
            except (OSError, ValueError, AttributeError):
                pass
    return False

"""One reading of an inspection attempt's evidence, shared by every caller.

Every finished, failed or interrupted attempt is exactly one outcome, in this precedence:

- FINDING: a judgment recorded anywhere names a concern. "Anywhere" is the reviewer's
  retained session, every captured client output (including archived earlier launches),
  the attempts this one continued, and the ledger's verdicts for the same input. It binds
  whatever the final answer says, and even if the attempt later failed.
- UNREADABLE: no concern was found, but some evidence cannot be read, so the absence of a
  concern is not established. It holds the range for a human, exactly like a finding.
- PASS: the last launch completed normally, its final answer is a valid pass of the exact
  batch with complete coverage, and the whole history used only the inspection's tools.
- NOT_JUDGED: everything else (unfinished, malformed output, a provider failure or timeout,
  an unexpected tool). It never binds.

A concern is a judgment's nonempty findings, or a `finding` label even without findings.
An `incomplete` label alone, with no findings, names no concern. Concerns are collected
before any validation, so an invalid attempt can never discard one.
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from .config import Die

FINDING, UNREADABLE, PASS, NOT_JUDGED = "finding", "unreadable", "pass", "not-judged"

# A client killed mid-write leaves a partial row. JSON escapes every quote inside a string, so
# these `"type": "..."`/`"role": "..."` pairs are the row's own structure, never reviewer text.
STRUCTURE = re.compile(r'"(?:type|role)":\s*"([^"\\]+)"')
# Rows of these kinds never hold a judgment: tool calls' outputs, reasoning, progress and input.
HARMLESS = frozenset(
    {
        "function_call_output",
        "custom_tool_call_output",
        "mcp_tool_call",
        "tool_result",
        "reasoning",
        "item.started",
        "item.updated",
        "event_msg",
        "user",
    }
)
ANSWERS = frozenset({"assistant", "agent_message", "result"})
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
READ_PAGE = "read_page"
# Codex offers its MCP resource helpers whenever an MCP server is configured. The only server is
# the read-only audit reader, which serves no resources: a call reads nothing and is inert.
INERT_TOOLS = frozenset({"list_mcp_resources", "list_mcp_resource_templates", "read_mcp_resource"})


class NotJudged(Die):
    """The attempt recorded no concern and no valid complete judgment. It never binds."""


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
    tools: list[str] = field(default_factory=list)  # Every tool request, normalized.
    unreadable: list[str] = field(default_factory=list)  # Why some evidence could not be read.
    damaged: list[str] = field(default_factory=list)  # Unreadable rows that cannot have held a judgment.

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


def tool_name(name) -> str:
    return READ_PAGE if name in ("read_page", "mcp__audit__read_page", "audit.read_page") else str(name)


def rows(path: Path, evidence: Evidence):
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        evidence.unreadable.append(f"{path}: {exc}")
        return
    for number, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError:
            row = None
        if not isinstance(row, dict):
            # A partial row that visibly cannot hold a judgment only prevents a pass. Any other
            # row could have held one, so it is unreadable evidence.
            kinds = set(STRUCTURE.findall(line))
            harmless = kinds & HARMLESS and not kinds & ANSWERS
            (evidence.damaged if harmless else evidence.unreadable).append(f"{path}:{number}: unreadable row")
            continue
        yield f"{path}:{number}", row


def read_row(row: dict, evidence: Evidence, where: str, messages: dict) -> None:
    """One row of a Codex native rollout, a Codex exec stream, or a Claude session or stream."""
    kind = row.get("type")
    if kind == "response_item":  # Codex native rollout: what a resume loads.
        item = row.get("payload")
        if not isinstance(item, dict):
            evidence.unreadable.append(f"{where}: unreadable native item")
        elif item.get("type") == "message" and item.get("role") == "assistant":
            parts = item.get("content")
            if not isinstance(parts, list) or any(not isinstance(p, dict) for p in parts):
                evidence.unreadable.append(f"{where}: unreadable native message")
                return
            text = "".join(str(p.get("text", "")) for p in parts if p.get("type") == "output_text")
            # Any phase but commentary is the answer itself, and must be a readable judgment.
            judgments_in(text, evidence, where, answer=item.get("phase") != "commentary")
        elif str(item.get("type", "")).endswith("_call"):
            evidence.tools.append(tool_name(item.get("name") or item.get("type")))
    elif kind == "event_msg":  # Codex native copies of the same messages; read them too.
        item = row.get("payload")
        if not isinstance(item, dict):
            evidence.unreadable.append(f"{where}: unreadable native event")
        elif item.get("type") == "agent_message":
            judgments_in(item.get("message"), evidence, where)
        elif item.get("type") == "task_complete" and item.get("last_agent_message") is not None:
            judgments_in(item["last_agent_message"], evidence, where)
    elif kind in ("item.started", "item.updated", "item.completed"):  # Codex exec stream.
        item = row.get("item")
        if not isinstance(item, dict):
            evidence.unreadable.append(f"{where}: unreadable client item")
        elif item.get("type") == "agent_message":
            # A message's updates hold partial text: read only its latest version, at the end of
            # the stream (`messages`), so an interrupted message is still read.
            key = item.get("id") or where
            if kind == "item.completed" or key not in messages or not messages[key][2]:
                messages[key] = (where, item.get("text"), kind == "item.completed")
        elif item.get("type") == "mcp_tool_call":
            server, tool = item.get("server"), item.get("tool")
            evidence.tools.append(tool_name(tool) if server == "audit" else f"{server}.{tool}")
        elif item.get("type") not in ("reasoning", "context_compaction"):
            evidence.tools.append(str(item.get("type")))
    elif kind == "assistant":  # Claude session or stream-json.
        message = row.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if isinstance(content, str):
            content = [{"type": "text", "text": content}]
        if not isinstance(content, list) or any(not isinstance(item, dict) for item in content):
            evidence.unreadable.append(f"{where}: unreadable assistant message")
            return
        for item in content:
            if item.get("type") == "tool_use" and item.get("name") == "StructuredOutput":
                judgments_in(item.get("input"), evidence, where, answer=True)
            elif item.get("type") == "tool_use":
                evidence.tools.append(tool_name(item.get("name")))
            elif item.get("type") == "text":
                judgments_in(item.get("text", ""), evidence, where)
    elif kind == "result":  # Claude's final result: its structured answer and its text.
        if row.get("structured_output") is not None:
            judgments_in(row["structured_output"], evidence, where, answer=True)
        if row.get("result") is not None:
            judgments_in(row["result"], evidence, where)


def collect(attempt: Path, evidence: Evidence | None = None, *, seen: set[Path] | None = None) -> Evidence:
    """Everything the attempt and the attempts it continued recorded."""
    from . import security_audit as sa

    evidence = Evidence() if evidence is None else evidence
    attempt = attempt.resolve()
    seen = set() if seen is None else seen
    if attempt in seen:
        evidence.unreadable.append(f"{attempt}: cyclic continuation lineage")
        return evidence
    seen.add(attempt)
    sources = [
        *sa.native_rollouts(attempt),
        *sa.claude_sessions(attempt),
        attempt / "model-transcript.jsonl",
        *sorted(attempt.glob("*/model-transcript.jsonl")),  # Archived earlier launches.
    ]
    for path in sources:
        if path.is_file():
            messages = {}
            for where, row in rows(path, evidence):
                read_row(row, evidence, where, messages)
            for where, text, _ in messages.values():
                judgments_in(text, evidence, where)
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


def recorded(state: Path, batch: dict, evidence: Evidence | None = None) -> Evidence:
    """The ledger's verdicts about this input or any of its commits.

    A concern recorded for a range binds every range containing any of its commits, until the
    cursor passes them: binding never depends on how ranges are later planned or split."""
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
    """The last launch's valid complete pass; raises with the reason when there is none."""
    from . import security_audit as sa

    if evidence.unreadable or evidence.damaged:
        raise Die("security-audit: retained history has unreadable rows")
    execution = json.loads((attempt / "execution.json").read_text())
    if execution.get("protocol_version") != sa.PROTOCOL:
        raise Die("security-audit: only a judgment-protocol-5 attempt can pass")
    provider = "claude" if execution["argv"][0] == "claude" else "codex"
    paged = execution["paged"]
    allowed = {READ_PAGE} if paged else set()
    if unexpected := sorted(set(evidence.tools) - allowed - INERT_TOOLS):
        raise Die(f"security-audit: reviewer attempted an unauthorized tool call: {', '.join(unexpected)}")
    transcript = (attempt / "model-transcript.jsonl").read_text()
    if provider == "claude":
        events = [json.loads(line) for line in transcript.splitlines() if line.strip()]
        if not paged and any(e.get("type") == "system" and e.get("subtype") == "compact_boundary" for e in events):
            raise Die("security-audit: inline source was compacted without a reader")
        value = sa.claude_result(transcript).get("structured_output")
    else:
        if len(sa.native_rollouts(attempt)) != 1:
            raise Die("security-audit: retained native history is missing or ambiguous")
        value = sa.codex_final(transcript, paged)
    if isinstance(value, dict) and value.get("verdict") == "unfinished":
        raise Die("security-audit: inspection unfinished")
    verdict = sa.bind_verdict(value, batch)
    if verdict["verdict"] != "pass":
        raise Die(f"security-audit: {verdict['verdict']} without a recorded concern")
    sa.validate_reads(verdict, batch, (attempt / "read-receipt.json").resolve(), paged)
    return verdict


def classify(state: Path, attempt: Path, batch: dict, failure: BaseException | str | None = None) -> Outcome:
    """The attempt's outcome. `failure` is why its last launch did not complete normally."""
    evidence = recorded(state, batch, collect(attempt))
    verdict = finding(batch, evidence)
    if verdict is not None:
        return Outcome(FINDING, verdict=verdict)
    if evidence.unreadable:
        return Outcome(UNREADABLE, reason="security-audit: unreadable evidence: " + "; ".join(evidence.unreadable[:3]))
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


def outcome_of(verdict, exc: BaseException | None) -> dict:
    """What a block records about how its attempt ended, so alerts never re-read the evidence."""
    if isinstance(verdict, dict) and verdict.get("verdict") != "pass":
        return {"outcome": FINDING, "verdict": verdict}
    return {"outcome": UNREADABLE if isinstance(exc, Unreadable) else NOT_JUDGED}


def engaged(attempt: Path) -> bool:
    """Whether the reviewer engaged with the input: it answered, or the reader served it a page.

    An attempt that is not a judgment is divided only then; a provider or setup failure, where
    no reviewer engaged, is retried on the same range."""
    if collect(attempt).judgments:
        return True
    try:
        return bool(json.loads((attempt / "read-receipt.json").read_text()).get("served"))
    except (OSError, ValueError, AttributeError):
        return False

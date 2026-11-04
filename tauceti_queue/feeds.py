"""The two Bors-compatible observation feeds, their schema fixture and its validator.

Bors serves two JSON documents that other automation reads and that the queue must keep serving
unchanged at the same paths (docs/redesign/specs/ai-merge-queue.md, "Policy status"):

- `GET /repositories/<n>/active-batches?base=<branch>[&batch_id=<id>]`, schema
  `tauceti-bors.observation/v1`: the active batches with members, the per-PR outcomes, the
  admin scope approvals and the requested batch. Read by TauCetiTools' `bors_observation.py` and
  `bors_scope_approval.py` (the scope-approval check needs `requested_batch` with state `running`,
  `head_sha`, `base_sha`, `members`, and `scope_approvals` records of exactly `{pr, actor_id,
  head_sha, merge_base_sha}`), and by `tauceti_worker/github.py` (`bump_repairs`).
- `GET /repositories/<n>/candidates?base=<branch>[&batch_id=<id>][&promoted=true]`, schema
  `tauceti-bors.candidates/v1`: the candidate journal (100 most recent candidates with their
  events and member snapshots, 500 most recent admissions). Read by the journal report and the
  repair experiments.

"Byte compatible" means the same top-level keys, the same field names and JSON types per record,
the same schema strings and the same timestamp units. `tests/fixtures/bors-feeds-schema.json` is
derived from saved production responses by `derive_schema` (regenerate with `python -m
tauceti_queue.feeds derive <observation.json> <candidates.json>`), and `validate` checks any
document against it: unknown keys and type changes are errors, so a drift in either direction is
caught by `tests/queue_feeds.py`.

The generators take a `FeedSource`, the read-only view the controller implements over its records
(`BatchView`, `OutcomeView`, `CandidateView`, ...), and produce the documents. Fields Bors derives
from its own internals and the queue has no counterpart for are populated with documented stand-ins
(docs/queue/github.md lists them field by field).
"""

from __future__ import annotations

import dataclasses
import json
import pathlib
import re
import sys
import time
from collections.abc import Sequence
from typing import Protocol

OBSERVATION_SCHEMA = "tauceti-bors.observation/v1"
CANDIDATES_SCHEMA = "tauceti-bors.candidates/v1"
CANDIDATE_CONTRACT = "tauceti-bors.candidate/v1"
LABEL_HINT_SCHEMA = "bors.scheduling-label-hint/v1"
TIMESTAMP_UNIT = "unix_microseconds"
HISTORICAL_UNKNOWN_REASON = "no journal backfill; absent facts are unknown"
CANDIDATE_LIMIT = 100
ADMISSION_LIMIT = 500
SCHEMA_FIXTURE = pathlib.Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "bors-feeds-schema.json"

# Paths whose object contents vary by record kind; the schema records them as open objects.
OPEN_PATHS = {"$.candidates[].events[].details"}
# Lists that were empty in every saved response, with the item shape taken from Bors's controller
# code (`api_controller.ex`, `bump_recovery.ex`) and the consumers that read them.
EMPTY_LIST_ITEMS = {
    "$.held[]": {
        "types": ["dict"],
        "keys": {"pr": {"types": ["int"]}, "head_sha": {"types": ["str"]}},
        "required": ["head_sha", "pr"],
    },
    "$.bump_repairs[]": {
        "types": ["dict"],
        "keys": {"pr": {"types": ["int"]}, "head_sha": {"types": ["str"]}, "batch_id": {"types": ["int"]}},
        "required": ["batch_id", "head_sha", "pr"],
        "open": True,
    },
    "$.handoff.released_github_prs[]": {"types": ["int"]},
}
# Where the queue knows what Bors did not: Bors always writes a string reason because commit statuses
# carry no runner lifecycle; the queue reads the lifecycle from the run's jobs and writes null then.
# Bors hardcodes the three runner lifecycle fields to null; the queue writes unix microseconds, the
# feed's timestamp unit. The top-level `batch_id` echo is an int for a `batch_id` query.
TYPE_WIDENINGS = {
    "$.candidates[].unknown_reason": ["null", "str"],
    "$.candidates[].runner_assigned_at": ["int", "null"],
    "$.candidates[].runner_started_at": ["int", "null"],
    "$.candidates[].runner_finished_at": ["int", "null"],
    "$.batch_id": ["int", "null"],
}
# `requested_batch` was null in every saved response (no `batch_id` query); Bors's `api_controller.ex`
# serves the same `batch_detail` record there as in `batches`, so the schema links the two.
LINKED_PATHS = {"$.requested_batch": "$.batches[]"}


# ---------------------------------------------------------------------------------------------
# Schema derivation and validation


def _json_type(value) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    if isinstance(value, str):
        return "str"
    if isinstance(value, list):
        return "list"
    if isinstance(value, dict):
        return "dict"
    raise TypeError(type(value))


class _Node:
    def __init__(self) -> None:
        self.types: set[str] = set()
        self.keys: dict[str, _Node] = {}
        self.required: set[str] | None = None
        self.items: _Node | None = None
        self.examples: list = []

    def observe(self, value, path: str) -> None:
        kind = _json_type(value)
        self.types.add(kind)
        if kind == "dict":
            if path in OPEN_PATHS:
                return
            present = set(value)
            self.required = present if self.required is None else self.required & present
            for key, item in value.items():
                self.keys.setdefault(key, _Node()).observe(item, f"{path}.{key}")
        elif kind == "list":
            if self.items is None:
                self.items = _Node()
            for item in value:
                self.items.observe(item, f"{path}[]")
        elif len(self.examples) < 2 and value not in self.examples and not isinstance(value, str | None):
            self.examples.append(value)

    def to_json(self, path: str) -> dict:
        out: dict = {"types": sorted(self.types)}
        if path in TYPE_WIDENINGS:
            out["types"] = sorted(set(out["types"]) | set(TYPE_WIDENINGS[path]))
        if "dict" in self.types:
            if path in OPEN_PATHS:
                out["open"] = True
            else:
                out["keys"] = {k: node.to_json(f"{path}.{k}") for k, node in sorted(self.keys.items())}
                out["required"] = sorted(self.required or ())
        if "list" in self.types:
            if self.items is not None and self.items.types:
                out["items"] = self.items.to_json(f"{path}[]")
            elif f"{path}[]" in EMPTY_LIST_ITEMS:
                out["items"] = EMPTY_LIST_ITEMS[f"{path}[]"]
        return out


def derive_schema(document: dict) -> dict:
    """A structural schema of one saved response: types per path, key sets and required keys."""
    root = _Node()
    root.observe(document, "$")
    schema = root.to_json("$")
    for target, origin in LINKED_PATHS.items():
        linked = _lookup(schema, origin)
        if linked is not None:
            node = dict(linked)
            node["types"] = sorted(set(node["types"]) | {"null"})
            _assign(schema, target, node)
    return schema


def _steps(path: str) -> list[str]:
    return [step for step in re.split(r"\.|(?=\[\])", path.removeprefix("$")) if step]


def _lookup(schema: dict, path: str) -> dict | None:
    node = schema
    for step in _steps(path):
        node = node.get("items") if step == "[]" else node.get("keys", {}).get(step)
        if node is None:
            return None
    return node


def _assign(schema: dict, path: str, value: dict) -> None:
    steps = _steps(path)
    node = schema
    for step in steps[:-1]:
        node = node["items"] if step == "[]" else node["keys"][step]
    if steps[-1] == "[]":
        node["items"] = value
    else:
        node["keys"][steps[-1]] = value


def validate(value, node: dict, path: str = "$") -> list[str]:
    """Every way `value` departs from `node`: wrong type, missing required key, unknown key."""
    errors: list[str] = []
    kind = _json_type(value)
    allowed = set(node.get("types", []))
    if kind == "int" and "float" in allowed and "int" not in allowed:
        kind = "float"
    if kind not in allowed:
        return [f"{path}: {kind} is not one of {sorted(allowed)}"]
    if kind == "dict" and not node.get("open"):
        keys = node.get("keys", {})
        for key in node.get("required", []):
            if key not in value:
                errors.append(f"{path}: missing required key {key!r}")
        for key, item in value.items():
            if key not in keys:
                errors.append(f"{path}: unknown key {key!r}")
            else:
                errors.extend(validate(item, keys[key], f"{path}.{key}"))
    elif kind == "list" and "items" in node:
        for index, item in enumerate(value):
            errors.extend(validate(item, node["items"], f"{path}[{index}]"))
    return errors


def load_schema(path: pathlib.Path = SCHEMA_FIXTURE) -> dict:
    return json.loads(path.read_text())


def validate_observation(document, schema: dict | None = None) -> list[str]:
    schema = schema or load_schema()
    errors = validate(document, schema["observation"])
    if isinstance(document, dict) and document.get("schema") != OBSERVATION_SCHEMA:
        errors.append(f"$.schema: {document.get('schema')!r} is not {OBSERVATION_SCHEMA!r}")
    return errors


def validate_candidates(document, schema: dict | None = None) -> list[str]:
    schema = schema or load_schema()
    errors = validate(document, schema["candidates"])
    if isinstance(document, dict):
        if document.get("schema") != CANDIDATES_SCHEMA:
            errors.append(f"$.schema: {document.get('schema')!r} is not {CANDIDATES_SCHEMA!r}")
        if document.get("timestamp_unit") != TIMESTAMP_UNIT:
            errors.append(f"$.timestamp_unit: {document.get('timestamp_unit')!r} is not {TIMESTAMP_UNIT!r}")
    return errors


# ---------------------------------------------------------------------------------------------
# The controller's view


@dataclasses.dataclass(frozen=True)
class MemberView:
    pr: int
    head_sha: str
    reviewer: str
    admitted_at: int  # unix microseconds of the admission that put this revision in the queue
    admission_id: int
    priority: int = 0
    labels: tuple[str, ...] = ()
    labels_observed_at: int = 0  # unix seconds
    scope_approval: dict | None = None  # {actor_id, head_sha, merge_base_sha} when an admin approved
    single: bool = False
    source: str = "queue.admission"
    retry_reason: str | None = None
    parent_batch_id: int | None = None
    parent_candidate_id: int | None = None
    parent_admission_id: int | None = None


@dataclasses.dataclass(frozen=True)
class BatchView:
    id: int
    base: str
    state: str  # waiting | running | ok | error | canceled
    sealed: bool
    head_sha: str | None
    base_sha: str | None
    members: tuple[MemberView, ...]
    candidate_id: int | None = None
    generation: int | None = None
    staging_ref: str | None = None
    promotion_failures: int = 0
    promotion_retry_at: int = 0
    recovery_failures: int = 0
    recovery_after: int = 0


@dataclasses.dataclass(frozen=True)
class OutcomeView:
    pr: int
    head_sha: str
    batch_id: int
    state: str


@dataclasses.dataclass(frozen=True)
class EventView:
    id: int
    kind: str
    source: str
    observed_at: int  # unix microseconds
    details: dict


@dataclasses.dataclass(frozen=True)
class CandidateView:
    id: int
    batch_id: int
    generation: int
    base: str
    base_sha: str
    members: tuple[MemberView, ...]
    observed_at: int  # unix microseconds
    events: tuple[EventView, ...] = ()
    previous_generation_id: int | None = None
    runner_assigned_at: int | None = None
    runner_started_at: int | None = None
    runner_finished_at: int | None = None


@dataclasses.dataclass(frozen=True)
class AdmissionView:
    id: int
    batch_id: int
    observed_at: int  # unix microseconds
    member: MemberView
    base: str


class FeedSource(Protocol):
    """What the controller exposes for the feeds; every method is a read of its records."""

    def repository(self, repo_id: int) -> str | None: ...

    def active_batches(self, repo: str, base: str) -> Sequence[BatchView]: ...

    def batch(self, repo: str, batch_id: int) -> BatchView | None: ...

    def outcomes(self, repo: str, base: str) -> Sequence[OutcomeView]: ...

    def scope_approvals(self, repo: str) -> Sequence[dict]: ...

    def candidates(self, repo: str, base: str, batch_id: int | None, *, promoted: bool) -> Sequence[CandidateView]: ...

    def admissions(self, repo: str, base: str, batch_id: int | None) -> Sequence[AdmissionView]: ...

    def started_at(self) -> float: ...


# ---------------------------------------------------------------------------------------------
# Generators


def _iso(timestamp: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(timestamp))


def _iso_micro(timestamp: float) -> str:
    whole = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(timestamp))
    return f"{whole}.{int((timestamp % 1) * 1_000_000):06d}Z"


def batch_detail(batch: BatchView) -> dict:
    train = None
    if batch.candidate_id is not None:
        train = {
            "candidate_id": batch.candidate_id,
            "generation": batch.generation if batch.generation is not None else 1,
            "predecessor_id": None,
            "staging_ref": batch.staging_ref or f"staging-candidate-{batch.candidate_id}-{batch.generation or 1}",
        }
    return {
        "id": batch.id,
        "base": batch.base,
        "state": batch.state,
        "sealed": batch.sealed,
        "head_sha": batch.head_sha,
        "base_sha": batch.base_sha,
        "promotion_retry_at": batch.promotion_retry_at,
        "promotion_failures": batch.promotion_failures,
        "recovery_after": batch.recovery_after,
        "recovery_failures": batch.recovery_failures,
        "train": train,
        "members": [{"pr": m.pr, "head_sha": m.head_sha} for m in batch.members],
    }


def observation_feed(
    source: FeedSource, repo: str, base: str, batch_id: int | None = None, *, now: float | None = None
) -> dict:
    """The `tauceti-bors.observation/v1` document for `repo` and `base`."""
    now = time.time() if now is None else now
    active = list(source.active_batches(repo, base))
    requested = None
    if batch_id is not None:
        batch = source.batch(repo, batch_id)
        if batch is not None and batch.base == base:
            requested = batch_detail(batch)
    approvals = []
    for approval in source.scope_approvals(repo):
        approvals.append(
            {
                "pr": int(approval["pr"]),
                "actor_id": int(approval["actor_id"]),
                "head_sha": str(approval["head_sha"]),
                "merge_base_sha": str(approval["merge_base_sha"]),
            }
        )
    seen: set[tuple[int, str]] = set()
    outcomes = []
    for outcome in source.outcomes(repo, base):
        key = (outcome.pr, outcome.head_sha)
        if key in seen:
            continue
        seen.add(key)
        outcomes.append(
            {"pr": outcome.pr, "head_sha": outcome.head_sha, "batch_id": outcome.batch_id, "state": outcome.state}
        )
    return {
        "schema": OBSERVATION_SCHEMA,
        "repo": repo,
        "base": base,
        "batch_ids": [b.id for b in active],
        "batches": [batch_detail(b) for b in active],
        "held": [],
        "bump_repairs": [],
        "outcomes": outcomes,
        "scope_approvals": approvals,
        "requested_batch": requested,
        "handoff": handoff(source, active, now),
    }


def handoff(source: FeedSource, active: Sequence[BatchView], now: float) -> dict:
    """Bors's merge-backend handoff block, with the queue as the only backend and nothing pending."""
    return {
        "pending": 0,
        "reason": "queue_service",
        "updated_at": _iso(source.started_at()),
        "backend": "queue",
        "observed_at": _iso_micro(now),
        "github_count": 0,
        "bors_count": len(active),
        "candidate_scan_complete": True,
        "candidate_snapshot_at": None,
        "eligible": [],
        "overlap": False,
        "released_github_prs": [],
    }


def member_snapshot(member: MemberView, base: str, *, assembly: bool) -> dict:
    snapshot = {
        "base": base,
        "bundle_id": None,
        "head_sha": member.head_sha,
        "manual_approval": (
            {
                "actor_id": int(member.scope_approval["actor_id"]),
                "head_sha": str(member.scope_approval["head_sha"]),
                "merge_base_sha": str(member.scope_approval["merge_base_sha"]),
            }
            if member.scope_approval
            else None
        ),
        "original_admitted_at": member.admitted_at,
        "parent_admission_id": member.parent_admission_id,
        "parent_batch_id": member.parent_batch_id,
        "parent_candidate_id": member.parent_candidate_id,
        "patch_id": member.pr,
        "pr": member.pr,
        "queue_age_retries": 0,
        "remainder_retries": 0,
        "retry_reason": member.retry_reason,
        "reviewer": member.reviewer,
        "scheduling": {
            "label_hint": {
                "binding": "live PR labels at admission; PR labels are mutable, not an atomic head read",
                "head_sha": member.head_sha,
                "labels": list(member.labels),
                "observed_at": member.labels_observed_at or member.admitted_at // 1_000_000,
                "pr": member.pr,
                "project_id": 1,
                "schema": LABEL_HINT_SCHEMA,
                "source": "github.pull_request/admission",
                "timestamp_unit": "unix_seconds",
            },
            "policy": "queue insertion; age-windowed random draw",
            "priority": member.priority,
            "singleton": member.single,
        },
        "source": member.source,
    }
    if assembly:
        # Candidate member snapshots name their admission and the head assembled; admission
        # snapshots predate both, as in Bors.
        snapshot["admission_id"] = member.admission_id
        snapshot["link_id"] = member.admission_id
        snapshot["assembly_head_sha"] = member.head_sha
    return snapshot


def candidate_record(candidate: CandidateView) -> dict:
    lifecycle_known = candidate.runner_started_at is not None or candidate.runner_finished_at is not None
    return {
        "id": candidate.id,
        "batch_id": candidate.batch_id,
        "generation": candidate.generation,
        "snapshot": {
            "base": candidate.base,
            "base_sha": candidate.base_sha,
            "contract": CANDIDATE_CONTRACT,
            "members": [member_snapshot(m, candidate.base, assembly=True) for m in candidate.members],
            "previous_generation_id": candidate.previous_generation_id,
            "speculative_predecessor_id": None,
        },
        "events": [
            {"id": e.id, "kind": e.kind, "source": e.source, "observed_at": e.observed_at, "details": dict(e.details)}
            for e in candidate.events
        ],
        "observed_at": candidate.observed_at,
        "runner_assigned_at": candidate.runner_assigned_at,
        "runner_started_at": candidate.runner_started_at,
        "runner_finished_at": candidate.runner_finished_at,
        "unknown_reason": None if lifecycle_known else "runner lifecycle not yet observed from the workflow run",
    }


def candidates_feed(
    source: FeedSource, repo: str, base: str, batch_id: int | None = None, *, promoted: bool = False
) -> dict:
    """The `tauceti-bors.candidates/v1` document for `repo` and `base`."""
    candidates = list(source.candidates(repo, base, batch_id, promoted=promoted))[:CANDIDATE_LIMIT]
    admissions = [] if promoted else list(source.admissions(repo, base, batch_id))[:ADMISSION_LIMIT]
    return {
        "schema": CANDIDATES_SCHEMA,
        "repo": repo,
        "base": base,
        "batch_id": batch_id,
        "candidates": [candidate_record(c) for c in candidates],
        "admissions": [
            {
                "id": a.id,
                "batch_id": a.batch_id,
                "link_id": a.id,
                "observed_at": a.observed_at,
                "snapshot": member_snapshot(a.member, a.base, assembly=False),
            }
            for a in admissions
        ],
        "historical_unknown_reason": HISTORICAL_UNKNOWN_REASON,
        "timestamp_unit": TIMESTAMP_UNIT,
        "queue_authority": False,
    }


# ---------------------------------------------------------------------------------------------
# Fixture maintenance


def reduce_sample(observation: dict, candidates: dict) -> dict:
    """A small cut of two saved responses that still exercises every record shape."""
    sample_observation = dict(observation)
    sample_observation["batches"] = observation["batches"][:2]
    sample_observation["batch_ids"] = [b["id"] for b in sample_observation["batches"]]
    for batch in sample_observation["batches"]:
        batch["members"] = batch["members"][:3]
    sample_observation["outcomes"] = observation["outcomes"][:5]
    sample_observation["handoff"] = dict(observation["handoff"], eligible=observation["handoff"]["eligible"][:2])
    sample_candidates = dict(candidates)
    picked = []
    for candidate in candidates["candidates"]:
        kinds = {e["kind"] for e in candidate["events"]}
        if len(picked) < 2 and ("promoted" in kinds or "failure_diagnostics" in kinds):
            trimmed = dict(candidate)
            trimmed["snapshot"] = dict(candidate["snapshot"], members=candidate["snapshot"]["members"][:2])
            trimmed["events"] = [e for e in candidate["events"] if len(json.dumps(e)) < 4000][:12]
            picked.append(trimmed)
    sample_candidates["candidates"] = picked
    sample_candidates["admissions"] = candidates["admissions"][:3]
    return {"observation": sample_observation, "candidates": sample_candidates}


def main(argv: Sequence[str]) -> int:
    if len(argv) != 4 or argv[1] != "derive":
        print("usage: python -m tauceti_queue.feeds derive <active-batches.json> <candidates.json>", file=sys.stderr)
        return 2
    observation = json.loads(pathlib.Path(argv[2]).read_text())
    candidates = json.loads(pathlib.Path(argv[3]).read_text())
    schema = {
        "derived_from": {"observation": pathlib.Path(argv[2]).name, "candidates": pathlib.Path(argv[3]).name},
        "observation": derive_schema(observation),
        "candidates": derive_schema(candidates),
    }
    SCHEMA_FIXTURE.write_text(json.dumps(schema, indent=1, sort_keys=True) + "\n")
    sample_path = SCHEMA_FIXTURE.with_name("bors-feeds-sample.json")
    sample_path.write_text(json.dumps(reduce_sample(observation, candidates), indent=None, sort_keys=True) + "\n")
    print(
        f"wrote {SCHEMA_FIXTURE} ({SCHEMA_FIXTURE.stat().st_size} bytes) and {sample_path} ({sample_path.stat().st_size} bytes)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))


# ---------------------------------------------------------------------------------------------
# The controller's records as a FeedSource


def feed_source(controller) -> FeedSource:
    """`controller` itself when it implements `FeedSource`, else a view over its records."""
    if hasattr(controller, "active_batches") and hasattr(controller, "candidates"):
        return controller
    return ControllerFeedSource(controller)


BATCH_STATES = {"open": "waiting", "head": "waiting", "landed": "ok", "removed": "canceled"}


def _micro(seconds) -> int:
    return int(round(float(seconds or 0) * 1_000_000))


class ControllerFeedSource:
    """`FeedSource` over `tauceti_queue.controller.Controller`'s store (inputs, batches, members,
    ci_attempts, journal), mapping its states to Bors's: the open batch is `waiting`, the head is
    `running` while a CI attempt is pending and `waiting` otherwise, a landed batch `ok`, a removed
    one `canceled`; a candidate is one CI attempt (generation = batch version); an admission is one
    member row with its input's approval evidence."""

    def __init__(self, controller) -> None:
        self.controller = controller

    @property
    def store(self):
        return self.controller.store

    def repository(self, repo_id: int) -> str | None:
        return self.controller.repository if repo_id == 1 else None

    def started_at(self) -> float:
        return float(getattr(self.controller, "started_at", 0.0) or 0.0)

    def _branch(self) -> str:
        return str(getattr(self.controller, "canonical_branch", "") or "main")

    def _serves(self, repo: str, base: str) -> bool:
        return repo == self.controller.repository and base == self._branch()

    # -- members and inputs

    def _inputs_by_id(self) -> dict[int, dict]:
        return {row["id"]: row for row in self.store.rows("inputs", {"repository": self.controller.repository})}

    def _member_view(self, member: dict, inputs: dict[int, dict]) -> MemberView:
        source = inputs.get(member["input_id"]) or {}
        evidence = _loads(source.get("evidence"), {})
        scope = None
        if evidence.get("admin") and evidence.get("merge_base_sha") and evidence.get("actor_id"):
            scope = {
                "actor_id": evidence["actor_id"],
                "head_sha": member["accepted_head"],
                "merge_base_sha": evidence["merge_base_sha"],
            }
        return MemberView(
            pr=int(member["number"]),
            head_sha=str(member["accepted_head"]),
            reviewer=str(source.get("reviewer") or evidence.get("actor_login") or ""),
            admitted_at=_micro(source.get("approved_at")),
            admission_id=int(member["id"]),
            priority=int(source.get("priority") or 0),
            labels=tuple(evidence.get("labels") or ()),
            labels_observed_at=int(float(source.get("approved_at") or 0)),
            scope_approval=scope,
            single=bool(evidence.get("single")),
            retry_reason=None,
        )

    def _members(self, batch_id: int, inputs: dict[int, dict]) -> tuple[MemberView, ...]:
        rows = self.store.rows("members", {"batch_id": batch_id, "removed": 0}, order="position")
        return tuple(self._member_view(m, inputs) for m in rows)

    # -- batches

    def _attempts(self, batch_id: int) -> list[dict]:
        return self.store.rows("ci_attempts", {"batch_id": batch_id}, order="id")

    def _batch_view(self, row: dict, inputs: dict[int, dict]) -> BatchView:
        attempts = self._attempts(row["id"])
        latest = attempts[-1] if attempts else None
        state = BATCH_STATES.get(row["state"], row["state"])
        if row["state"] == "head" and latest is not None and latest.get("outcome") == "pending":
            state = "running"
        sealed = bool(row["sealed"])
        return BatchView(
            id=int(row["id"]),
            base=self._branch(),
            state=state,
            sealed=sealed,
            head_sha=row["tip"] if sealed else None,
            base_sha=row["base"] if sealed else None,
            members=self._members(row["id"], inputs),
            candidate_id=int(latest["id"]) if latest else None,
            generation=int(latest["batch_version"]) if latest else None,
            staging_ref=self.controller.staging_candidate_ref(int(latest["id"]), int(latest["batch_version"]))
            if latest and hasattr(self.controller, "staging_candidate_ref")
            else None,
            promotion_failures=int(row.get("promotion_failures") or 0),
        )

    def active_batches(self, repo: str, base: str) -> Sequence[BatchView]:
        if not self._serves(repo, base):
            return []
        inputs = self._inputs_by_id()
        rows = [r for r in self.controller.batches() if r["state"] in ("open", "head")]
        rows.sort(key=lambda r: r["id"], reverse=True)
        return [self._batch_view(r, inputs) for r in rows]

    def batch(self, repo: str, batch_id: int) -> BatchView | None:
        if repo != self.controller.repository:
            return None
        row = self.store.row("batches", {"id": batch_id, "repository": repo})
        return self._batch_view(row, self._inputs_by_id()) if row else None

    def outcomes(self, repo: str, base: str) -> Sequence[OutcomeView]:
        if not self._serves(repo, base):
            return []
        batches = {b["id"]: b for b in self.controller.batches()}
        rows = self.store.rows("members", None, order="id DESC", limit=5000)
        inputs = self._inputs_by_id()
        out = []
        for member in rows:
            batch = batches.get(member["batch_id"])
            if batch is None:
                continue
            state = BATCH_STATES.get(batch["state"], batch["state"])
            if member["removed"]:
                state = "canceled"
            elif batch["state"] == "head":
                attempts = self._attempts(batch["id"])
                state = "running" if attempts and attempts[-1].get("outcome") == "pending" else "waiting"
            source = inputs.get(member["input_id"]) or {}
            if source.get("state") == "landed" and batch["state"] != "landed":
                continue
            out.append(OutcomeView(int(member["number"]), str(member["accepted_head"]), int(batch["id"]), state))
        out.sort(key=lambda o: o.batch_id, reverse=True)
        return out

    def scope_approvals(self, repo: str) -> Sequence[dict]:
        if repo != self.controller.repository:
            return []
        approvals = []
        for row in self.store.rows("inputs", {"repository": repo}, order="id"):
            if row["state"] not in ("waiting", "reserved", "inserted"):
                continue
            evidence = _loads(row.get("evidence"), {})
            if evidence.get("admin") and evidence.get("merge_base_sha") and evidence.get("actor_id"):
                approvals.append(
                    {
                        "pr": int(row["number"]),
                        "actor_id": int(evidence["actor_id"]),
                        "head_sha": str(row["head_sha"]),
                        "merge_base_sha": str(evidence["merge_base_sha"]),
                    }
                )
        return approvals

    # -- candidates and admissions

    def _events(self, batch_id: int, attempt: dict) -> tuple[EventView, ...]:
        entries = self.store.rows("journal", {"batch_id": batch_id}, order="id")
        events = []
        for entry in entries:
            detail = _loads(entry.get("detail"), {})
            if detail.get("attempt_id") not in (None, attempt["id"]):
                continue
            events.append(
                EventView(int(entry["id"]), str(entry["kind"]), "queue.controller", _micro(entry["at"]), detail)
            )
        return tuple(events)

    def candidates(self, repo: str, base: str, batch_id: int | None, *, promoted: bool) -> Sequence[CandidateView]:
        if not self._serves(repo, base):
            return []
        inputs = self._inputs_by_id()
        batches = {b["id"]: b for b in self.controller.batches()}
        where = {"batch_id": batch_id} if batch_id is not None else None
        rows = self.store.rows("ci_attempts", where, order="id DESC", limit=CANDIDATE_LIMIT)
        out = []
        for attempt in rows:
            batch = batches.get(attempt["batch_id"])
            if batch is None or (promoted and batch["state"] != "landed"):
                continue
            previous = self.store.rows("ci_attempts", {"batch_id": attempt["batch_id"]}, order="id")
            earlier = [a["id"] for a in previous if a["id"] < attempt["id"]]
            out.append(
                CandidateView(
                    id=int(attempt["id"]),
                    batch_id=int(attempt["batch_id"]),
                    generation=int(attempt["batch_version"]),
                    base=self._branch(),
                    base_sha=str(attempt["base_sha"]),
                    members=self._members(attempt["batch_id"], inputs),
                    observed_at=_micro(attempt["dispatched_at"]),
                    events=self._events(attempt["batch_id"], attempt),
                    previous_generation_id=earlier[-1] if earlier else None,
                    runner_assigned_at=_micro(attempt["run_created_at"]) if attempt.get("run_created_at") else None,
                    runner_started_at=_micro(attempt["run_started_at"]) if attempt.get("run_started_at") else None,
                    runner_finished_at=_micro(attempt["run_completed_at"]) if attempt.get("run_completed_at") else None,
                )
            )
        return out

    def admissions(self, repo: str, base: str, batch_id: int | None) -> Sequence[AdmissionView]:
        if not self._serves(repo, base):
            return []
        inputs = self._inputs_by_id()
        where = {"batch_id": batch_id} if batch_id is not None else None
        rows = self.store.rows("members", where, order="id DESC", limit=ADMISSION_LIMIT)
        return [
            AdmissionView(int(m["id"]), int(m["batch_id"]), _micro(m["added_at"]), self._member_view(m, inputs), base)
            for m in rows
        ]


def _loads(text, default):
    if not text:
        return default
    try:
        value = json.loads(text) if isinstance(text, str) else text
    except ValueError:
        return default
    return value if isinstance(value, type(default)) else default

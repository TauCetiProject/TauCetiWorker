"""Import of Bors's state at the cutover (plan, "Transition to production", step 3).

Bors's observation feed (`tauceti-bors.observation/v1`) lists its active batches with their state
(`waiting` or `running`), sealed flag and members (`pr`, `head_sha`), but no admission times,
reviewers or priorities. Those come from the candidate journal (`tauceti-bors.candidates/v1`):
`admissions[].snapshot` and `candidates[].snapshot.members[]` both carry `original_admitted_at`
(unix microseconds), `reviewer` and `scheduling.priority` per (PR, head). The journal is bounded
(the 500 most recent admissions and 100 most recent candidates), so several saved journals can be
given; a member none of them covers keeps the import time as its age and says so in its evidence.

Rules (`run`):

- every member of a batch that is not running (waiting, sealed or not) becomes an approved input at
  its head with its original admission age, reviewer and priority, evidence `{"source":
  "bors-import", "batch_id", "feed_observed_at", ...}`, after the live PR is validated
  (`Controller.validate_pull`: open, not a draft, on the canonical branch, head unchanged);
- the members of Bors's running head are recorded as the controller's external head, not as
  inputs; the controller seals no head until they land (observed as a canonical advance made of
  Bors's `Source-PR`-less squash commits, or as Bors closing the members with its merged title) or
  the head is released: by a re-import whose feed no longer shows the batch running, or by the
  operator (`release_head`); released members become approved inputs with their ages;
- re-running changes nothing already present (same PR and head) and reports the counts;
- a dry run validates and reports without writing.

`tauceti-queue import-bors` is the operator command (`tauceti_queue.__main__`), which reads the
documents from files or URLs and posts them to the service's `/admin/import-bors` endpoint.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import pathlib
import sys
import urllib.request
from collections.abc import Iterable, Sequence
from typing import Any

from . import chain, feeds
from .eligibility import REVIEWER as REVIEW_APP_REVIEWER

log = logging.getLogger("tauceti_queue.bors_import")

SOURCE = "bors-import"
MICRO = 1_000_000
INPUT_LIVE_STATES = ("waiting", "reserved", "inserted", "landed")
PREFETCH_CHUNK = 200


@dataclasses.dataclass(frozen=True)
class AdmissionFacts:
    """What the candidate journal knows about one (PR, head): age, reviewer, priority."""

    approved_at: float | None
    reviewer: str | None
    priority: int
    admission_id: int | None
    manual_approval: dict[str, Any] | None = None


@dataclasses.dataclass
class Candidate:
    """A member of a non-running Bors batch, or a released member of its head, to become an input."""

    pr: int
    head_sha: str
    batch_id: int | None
    batch_state: str
    sealed: bool


def _micro_to_seconds(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value) / MICRO


def _merge_facts(index: dict[tuple[int, str], AdmissionFacts], snapshot: dict[str, Any], admission_id: Any) -> None:
    pr, head = snapshot.get("pr"), snapshot.get("head_sha")
    if not isinstance(pr, int) or not isinstance(head, str) or not head:
        return
    approved_at = _micro_to_seconds(snapshot.get("original_admitted_at"))
    reviewer = snapshot.get("reviewer") if isinstance(snapshot.get("reviewer"), str) else None
    scheduling = snapshot.get("scheduling") if isinstance(snapshot.get("scheduling"), dict) else {}
    priority = scheduling.get("priority") if isinstance(scheduling.get("priority"), int) else 0
    manual = snapshot.get("manual_approval") if isinstance(snapshot.get("manual_approval"), dict) else None
    key = (pr, head)
    existing = index.get(key)
    if existing is None:
        index[key] = AdmissionFacts(
            approved_at, reviewer, priority, admission_id if isinstance(admission_id, int) else None, manual
        )
        return
    # Retries repeat the original admission time; keep the earliest seen and fill in what was missing.
    times = [t for t in (existing.approved_at, approved_at) if t is not None]
    index[key] = AdmissionFacts(
        min(times) if times else None,
        existing.reviewer or reviewer,
        existing.priority or priority,
        existing.admission_id if existing.admission_id is not None else admission_id,
        existing.manual_approval or manual,
    )


def admission_index(candidates: Any) -> dict[tuple[int, str], AdmissionFacts]:
    """Ages, reviewers and priorities by (PR, head) from one candidate journal or several."""
    documents = [candidates] if isinstance(candidates, dict) else list(candidates or [])
    index: dict[tuple[int, str], AdmissionFacts] = {}
    for document in documents:
        if not isinstance(document, dict):
            continue
        for admission in document.get("admissions") or []:
            if isinstance(admission, dict) and isinstance(admission.get("snapshot"), dict):
                _merge_facts(index, admission["snapshot"], admission.get("id"))
        for candidate in document.get("candidates") or []:
            snapshot = candidate.get("snapshot") if isinstance(candidate, dict) else None
            for member in (snapshot or {}).get("members") or []:
                if isinstance(member, dict):
                    _merge_facts(index, member, member.get("admission_id"))
    return index


def reduce_candidates(document: dict[str, Any]) -> dict[str, Any]:
    """The part of a candidate journal the import reads, small enough to post to the service."""
    index = admission_index(document)
    admissions = [
        {
            "id": facts.admission_id,
            "snapshot": {
                "pr": pr,
                "head_sha": head,
                "reviewer": facts.reviewer,
                "original_admitted_at": None if facts.approved_at is None else int(round(facts.approved_at * MICRO)),
                "scheduling": {"priority": facts.priority},
                "manual_approval": facts.manual_approval,
            },
        }
        for (pr, head), facts in sorted(index.items())
    ]
    return {
        "schema": document.get("schema"),
        "repo": document.get("repo"),
        "base": document.get("base"),
        "timestamp_unit": document.get("timestamp_unit"),
        "admissions": admissions,
        "candidates": [],
    }


def load_document(spec: str, timeout: float = 60) -> dict[str, Any]:
    """A JSON document from a file path or an http(s) URL."""
    if spec.startswith(("http://", "https://")):
        request = urllib.request.Request(spec, headers={"Accept": "application/json", "User-Agent": "tauceti-queue"})
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - operator-supplied URL
            data = json.loads(response.read().decode())
    else:
        data = json.loads(pathlib.Path(spec).expanduser().read_text())
    if not isinstance(data, dict):
        raise ValueError(f"{spec}: expected a JSON object")
    return data


def _check_observation(observation: dict[str, Any], repository: str, branch: str) -> None:
    from .controller import ControllerError

    if observation.get("schema") != feeds.OBSERVATION_SCHEMA:
        raise ControllerError("feed-schema", f"expected {feeds.OBSERVATION_SCHEMA}, got {observation.get('schema')!r}")
    if observation.get("repo") != repository:
        raise ControllerError("feed-repository", f"the feed describes {observation.get('repo')!r}, not {repository}")
    base = observation.get("base")
    if base not in (None, branch):
        raise ControllerError("feed-base", f"the feed describes base {base!r}, not {branch}")
    if not isinstance(observation.get("batches"), list):
        raise ControllerError("feed-shape", "`batches` must be a list")


def _members(batch: dict[str, Any]) -> list[tuple[int, str]]:
    members = []
    for member in batch.get("members") or []:
        if isinstance(member, dict) and isinstance(member.get("pr"), int) and isinstance(member.get("head_sha"), str):
            members.append((member["pr"], member["head_sha"]))
    return members


def _observed_at(observation: dict[str, Any]) -> str:
    handoff = observation.get("handoff") if isinstance(observation.get("handoff"), dict) else {}
    return str(handoff.get("observed_at") or "")


def run(
    controller,
    observation: dict[str, Any],
    candidates: Any = None,
    *,
    now: float,
    dry_run: bool = False,
    release_head: bool = False,
) -> dict[str, Any]:
    """Apply (or, in a dry run, describe) the import rules above against `controller`."""
    repository = controller.repository
    _check_observation(observation, repository, controller.canonical_branch)
    index = admission_index(candidates)
    observed_at = _observed_at(observation)
    batches = [b for b in observation["batches"] if isinstance(b, dict) and isinstance(b.get("id"), int)]
    running = [b for b in batches if b.get("state") == "running"]
    others = [b for b in batches if b.get("state") != "running"]
    report: dict[str, Any] = {
        "dry_run": dry_run,
        "repository": repository,
        "feed_observed_at": observed_at,
        "ages_known_for": len(index),
        "batches": {
            "running": len(running),
            "waiting_sealed": sum(1 for b in others if b.get("sealed")),
            "waiting_unsealed": sum(1 for b in others if not b.get("sealed")),
        },
        "members": {"total": sum(len(_members(b)) for b in batches), "external_head": 0, "candidates": 0},
        "imported": 0,
        "present": 0,
        "refused": 0,
        "refusals": [],
        "ages": {"from_candidates": 0, "import_time": 0},
        "external_head": None,
        "released_from_head": 0,
    }

    # -- Bors's running head
    seen: set[tuple[int, str]] = set()
    queue: list[Candidate] = []
    for batch in others:
        for pr, head in _members(batch):
            if (pr, head) in seen:
                continue
            seen.add((pr, head))
            queue.append(
                Candidate(pr, head, batch["id"], str(batch.get("state") or "waiting"), bool(batch.get("sealed")))
            )

    existing = controller.external_head()
    head_batch = running[0] if running else None
    if head_batch is not None:
        if len(running) > 1:
            log.warning(
                "the feed lists %d running batches; only %s is recorded as Bors's head", len(running), head_batch["id"]
            )
        members = _members(head_batch)
        report["members"]["external_head"] = len(members)
        record = {
            "batch_id": head_batch["id"],
            "head_sha": head_batch.get("head_sha"),
            "base_sha": head_batch.get("base_sha"),
            "members": [{"pr": pr, "head_sha": head} for pr, head in members],
            "feed_observed_at": observed_at,
        }
        if existing is not None and existing.get("batch_id") == head_batch["id"] and existing["state"] == "running":
            report["external_head"] = {"batch_id": head_batch["id"], "members": len(members), "action": "unchanged"}
        elif release_head:
            report["external_head"] = {"batch_id": head_batch["id"], "members": len(members), "action": "released"}
            for pr, head in members:
                if (pr, head) not in seen:
                    seen.add((pr, head))
                    queue.append(Candidate(pr, head, head_batch["id"], "running", True))
                    report["released_from_head"] += 1
        else:
            report["external_head"] = {"batch_id": head_batch["id"], "members": len(members), "action": "recorded"}
            if not dry_run:
                if existing is not None and existing["state"] == "running":
                    controller.release_external_head(f"replaced by batch {head_batch['id']}", now)
                controller.record_external_head(record, now)
    if existing is not None and existing["state"] == "running" and (head_batch is None or release_head):
        # Bors's head is no longer running (it landed, failed or was cancelled) or the operator released it:
        # its members that did not land are released back to approved inputs with their ages.
        reason = "operator release" if release_head else "no longer running in the feed"
        report["external_head"] = {
            "batch_id": existing.get("batch_id"),
            "members": len(existing.get("members", [])),
            "action": "released",
            "reason": reason,
        }
        landed = set(existing.get("landed_members", []))
        for member in existing.get("members", []):
            pr, head = int(member["pr"]), str(member["head_sha"])
            if pr in landed or (pr, head) in seen:
                continue
            seen.add((pr, head))
            queue.append(Candidate(pr, head, existing.get("batch_id"), "released", True))
            report["released_from_head"] += 1
        if not dry_run:
            controller.release_external_head(reason, now)
    report["members"]["candidates"] = len(queue)

    # -- the approved inputs
    todo: list[Candidate] = []
    for candidate in queue:
        row = controller.input_for(repository, candidate.pr)
        if row is not None and row["head_sha"] == candidate.head_sha and row["state"] in INPUT_LIVE_STATES:
            report["present"] += 1
            continue
        todo.append(candidate)
    remaining, reset_at = controller.github.rate_limit()
    report["api_budget"] = {"needed": len(todo), "remaining": remaining, "reset_at": reset_at}
    if todo and remaining < len(todo) + 100:
        from .controller import ControllerError

        raise ControllerError(
            "rate-limit",
            f"{len(todo)} pull requests to validate but only {remaining} API calls remain until {reset_at:.0f}",
        )
    if todo:
        _prefetch_heads(controller, todo)
    for candidate in todo:
        pull, reason = controller.validate_pull(repository, candidate.pr, candidate.head_sha)
        if reason is None and not chain.object_exists(controller.clone, candidate.head_sha):
            reason = "head-unavailable"
        if reason is not None:
            _refuse(report, candidate, reason)
            continue
        facts = index.get((candidate.pr, candidate.head_sha))
        approved_at = facts.approved_at if facts is not None else None
        age_source = "candidates" if approved_at is not None else "import-time"
        report["ages"]["from_candidates" if approved_at is not None else "import_time"] += 1
        reviewer = (facts.reviewer if facts is not None else None) or REVIEW_APP_REVIEWER
        evidence: dict[str, Any] = {
            "source": SOURCE,
            "batch_id": candidate.batch_id,
            "feed_observed_at": observed_at,
            "batch_state": candidate.batch_state,
            "sealed": candidate.sealed,
            "age_source": age_source,
            "reviewer_source": "candidates" if facts is not None and facts.reviewer else "assumed",
            "head_sha": candidate.head_sha,
        }
        if facts is not None and facts.admission_id is not None:
            evidence["admission_id"] = facts.admission_id
        manual = facts.manual_approval if facts is not None else None
        if manual and manual.get("head_sha") == candidate.head_sha and manual.get("merge_base_sha"):
            # An administrator's scope approval travels with the input so the observation feed keeps serving it.
            evidence.update(admin=True, actor_id=manual.get("actor_id"), merge_base_sha=manual.get("merge_base_sha"))
        if dry_run:
            report["imported"] += 1
            continue
        result = controller.approve(
            repository,
            candidate.pr,
            candidate.head_sha,
            reviewer,
            facts.priority if facts is not None else 0,
            evidence,
            approved_at=approved_at if approved_at is not None else now,
            pull=pull,
        )
        if result["accepted"]:
            report["imported"] += 1
        else:
            _refuse(report, candidate, result["reason"])
    if not dry_run:
        controller.store.journal(
            now,
            "bors-import",
            imported=report["imported"],
            present=report["present"],
            refused=report["refused"],
            external_head=report["external_head"],
            feed_observed_at=observed_at,
        )
    return report


def _refuse(report: dict[str, Any], candidate: Candidate, reason: str) -> None:
    report["refused"] += 1
    report["refusals"].append(
        {"pr": candidate.pr, "head_sha": candidate.head_sha, "batch_id": candidate.batch_id, "reason": reason}
    )


def _prefetch_heads(controller, candidates: Sequence[Candidate]) -> None:
    """One fetch per chunk of pull refs instead of one per PR; failures leave `head-unavailable` to report."""
    missing = [c for c in candidates if not chain.object_exists(controller.clone, c.head_sha)]
    for start in range(0, len(missing), PREFETCH_CHUNK):
        chunk = missing[start : start + PREFETCH_CHUNK]
        refspecs = [f"+refs/pull/{c.pr}/head:{chain.REF_NAMESPACE}/pulls/{c.pr}" for c in chunk]
        try:
            controller.github.fetch_refs(controller.repository, refspecs)
        except Exception as error:  # noqa: BLE001 - reported per PR as head-unavailable
            log.warning("prefetch of %d pull refs failed: %s", len(chunk), error)


def format_report(report: dict[str, Any]) -> str:
    """The one-screen rendering `tauceti-queue import-bors` prints."""
    mode = "dry run: would import" if report.get("dry_run") else "imported"
    batches = report.get("batches") or {}
    members = report.get("members") or {}
    ages = report.get("ages") or {}
    lines = [
        f"feed observed at {report.get('feed_observed_at') or '?'} for {report.get('repository')}: "
        f"{batches.get('running', 0)} running, {batches.get('waiting_sealed', 0)} waiting sealed, "
        f"{batches.get('waiting_unsealed', 0)} waiting unsealed batches; {members.get('total', 0)} members",
        f"{mode} {report.get('imported', 0)} approved inputs "
        f"({ages.get('from_candidates', 0)} with their Bors age, {ages.get('import_time', 0)} aged from now); "
        f"{report.get('present', 0)} already present; {report.get('refused', 0)} refused",
    ]
    head = report.get("external_head")
    if head:
        lines.append(
            f"Bors's running head: batch {head.get('batch_id')} with {head.get('members')} members, {head.get('action')}"
            + (f" ({head['reason']})" if head.get("reason") else "")
        )
        if report.get("released_from_head"):
            lines.append(f"  {report['released_from_head']} members released from it to approved inputs")
    else:
        lines.append("Bors's running head: none in the feed")
    for refusal in report.get("refusals") or []:
        lines.append(f"  refused #{refusal['pr']} at {str(refusal['head_sha'])[:12]}: {refusal['reason']}")
    budget = report.get("api_budget")
    if budget:
        lines.append(f"API budget: {budget.get('needed')} reads needed, {budget.get('remaining')} remaining")
    return "\n".join(lines)


# ---------------------------------------------------------------------------------------------
# Fixture reduction (`python -m tauceti_queue.bors_import reduce <observation.json> <candidates.json>...`)


def reduce_fixture(observation: dict[str, Any], candidates: Iterable[dict[str, Any]], per_batch: int = 6) -> dict:
    """A small cut of saved production responses that keeps every batch and a few members of each.

    Members whose age the journals know are preferred, with at least one unknown kept per batch
    when there is one, so the fixture exercises both age sources.
    """
    index = admission_index(list(candidates))
    reduced = dict(observation)
    kept: set[tuple[int, str]] = set()
    scoped = {(s.get("pr"), s.get("head_sha")) for s in observation.get("scope_approvals") or []}
    batches = []
    for batch in observation["batches"]:
        members = _members(batch)
        # An administrator's scope approval is the rarest record; keep every member that carries one.
        known = sorted((m for m in members if m in index), key=lambda m: m not in scoped)
        unknown = [m for m in members if m not in index]
        chosen = known[: per_batch - (1 if unknown else 0)]
        chosen += unknown[: per_batch - len(chosen)]
        chosen += [m for m in known if m not in chosen][: per_batch - len(chosen)]
        kept.update(chosen)
        batches.append(dict(batch, members=[{"pr": pr, "head_sha": head} for pr, head in chosen]))
    reduced["batches"] = batches
    reduced["batch_ids"] = [b["id"] for b in batches]
    reduced["outcomes"] = [o for o in observation.get("outcomes", []) if (o.get("pr"), o.get("head_sha")) in kept]
    handoff = dict(observation.get("handoff") or {})
    handoff["eligible"] = [e for e in handoff.get("eligible") or [] if any(e.get("pr") == pr for pr, _ in kept)][:5]
    reduced["handoff"] = handoff
    reduced["scope_approvals"] = [
        s for s in observation.get("scope_approvals") or [] if (s.get("pr"), s.get("head_sha")) in kept
    ]
    admissions = [
        {
            "id": facts.admission_id,
            "batch_id": None,
            "snapshot": {
                "pr": pr,
                "head_sha": head,
                "reviewer": facts.reviewer,
                "original_admitted_at": None if facts.approved_at is None else int(round(facts.approved_at * MICRO)),
                "scheduling": {"priority": facts.priority, "singleton": False},
                "manual_approval": facts.manual_approval,
                "source": "bors.admission",
            },
        }
        for (pr, head), facts in sorted(index.items())
        if (pr, head) in kept
    ]
    reduced_candidates = {
        "schema": feeds.CANDIDATES_SCHEMA,
        "repo": observation.get("repo"),
        "base": observation.get("base"),
        "timestamp_unit": feeds.TIMESTAMP_UNIT,
        "admissions": admissions,
        "candidates": [],
    }
    return {"observation": reduced, "candidates": reduced_candidates}


def main(argv: Sequence[str]) -> int:
    if len(argv) < 3 or argv[1] != "reduce":
        print(
            "usage: python -m tauceti_queue.bors_import reduce <observation.json> <candidates.json>...", file=sys.stderr
        )
        return 2
    observation = json.loads(pathlib.Path(argv[2]).read_text())
    candidates = [json.loads(pathlib.Path(path).read_text()) for path in argv[3:]]
    print(json.dumps(reduce_fixture(observation, candidates), indent=1, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

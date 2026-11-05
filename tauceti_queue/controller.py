"""The AI merge queue controller: records, claims, insertion, sealing, head reconciliation, tasks, status.

This is increment 1 of `docs/redesign/specs/ai-merge-queue.md`: one open batch receiving scripted
insertions and at most one sealed head that is rebased, built, repaired in place by agents claiming
tasks, and fast-forwarded onto the canonical branch. The controller is one object shared by the
reconciliation loop (`tauceti_queue.loop`) and the HTTP server; every public method takes the
controller lock, and every record mutation is a `Store` transaction.

The controller never calls GitHub itself; it is handed a `GitHubPort`. Git work (squash, replay,
validation, provenance) is `tauceti_queue.chain` run against a local clone of the content
repository, into which the port fetches member heads and the canonical branch. GitHub side effects
(promotion, comments, closes, labels, ref retention) go through the outbox table with idempotency
keys and are drained with retries; restart reconciliation (`recover`) releases in-process claims and
leaves the outbox and worker claims to be re-driven.

Documentation of the records, state machine, task API and status schema: `docs/queue/controller.md`.
"""

from __future__ import annotations

import logging
import random
import re
import secrets
import threading
import time
from collections.abc import Callable, Sequence
from typing import Any

from . import chain
from .eligibility import KEEP as BLOCKING_LABELS
from .ports import (
    AGENT_INSTRUCTION,
    Adoption,
    GitHubPort,
    GitHubUnavailable,
    Task,
    TaskMember,
)
from .stale_cache import StaleCache
from .store import Store, dumps, loads

STATUS_SCHEMA = "tauceti-queue.status/v1"
BUILD_CONTEXT = "build"
# The commit statuses pr-build.yml's finalizer posts on the candidate head, every one of which Bors
# required (`bors.toml` `status`): `build` is the build's result, `bump-guard` the Lake-pin validation
# and `scope` the merge-policy verdict (restricted paths, diff size, oversized new files). A head
# lands only when all three are bound to its run and `success`.
VERDICT_CONTEXTS = (BUILD_CONTEXT, "bump-guard", "scope")
LOG_GRACE_SECONDS = 600  # how long a failed run may go without a readable log before it counts as infrastructure
DISPATCH_EVENT = "tauceti-bors-staging"
MERGED_TITLE_PREFIX = "[Merged by Bors] - "  # what bors-ng writes when it closes a squash-landed member
COMMENT_MARKER = "tauceti-queue"

# Settings an operator can change at runtime (`set_setting`), with their initial values. The type of
# the initial value is the type a new value must have.
DEFAULT_SETTINGS: dict[str, Any] = {
    "insertion_age_window": 16,
    "open_batch_limit": 2,
    "repair_concurrency": 1,
    "stall_interval_seconds": 900,
    "publication_enabled": False,
    "claim_ttl_seconds": 900,
    "dispatch_timeout_seconds": 600,
    "lost_run_seconds": 3600,
    "infrastructure_retry_limit": 3,
    "infrastructure_retry_backoff_seconds": 60,
    "inference_failure_threshold": 3,
    "inference_hold_backoff_seconds": 60,
    "inference_hold_backoff_max_seconds": 3600,
    "outbox_retry_seconds": 30,
    "outbox_retry_max_seconds": 900,
    "polling_interval_seconds": 60,
    "approval_poll_interval_seconds": 900,
    "membership_label": "bors-staging",
    # Wall-clock bounds on one tick's work, so the controller lock is not held for minutes while the
    # public endpoints wait on it: insertion stops probing and the outbox stops draining once the
    # budget is spent, and the loop is woken to continue in the next tick.
    "insertion_probe_budget_seconds": 20,
    "outbox_drain_budget_seconds": 20,
}

# How long `status()` waits for the controller lock before serving the last snapshot instead.
STATUS_LOCK_WAIT_SECONDS = 2.0
# How long one `repository_admin(login)` answer stands; served stale while GitHub cannot answer.
ADMIN_CACHE_SECONDS = 6 * 3600.0

INPUT_WAITING = "waiting"
INPUT_RESERVED = "reserved"
INPUT_INSERTED = "inserted"
INPUT_LANDED = "landed"
INPUT_CANCELLED = "cancelled"

BATCH_OPEN = "open"
BATCH_HEAD = "head"
BATCH_LANDED = "landed"
BATCH_REMOVED = "removed"

TASK_OPEN = "open"
TASK_SUBMITTED = "submitted"
TASK_SUPERSEDED = "superseded"

OUTCOME_PENDING = "pending"
OUTCOME_PASS = "pass"
OUTCOME_SOURCE_FAILURE = "source-failure"
OUTCOME_INFRASTRUCTURE = "infrastructure"
OUTCOME_SUPERSEDED = "superseded"

_PR_MENTION_RE = re.compile(r"(?:(?P<repo>[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+))?#(?P<number>\d+)")
# The subject of a squash commit bors-ng lands: `<title> (#<number>)`, with no `Source-PR` trailer.
_BORS_SUBJECT_RE = re.compile(r"\(#(?P<number>\d+)\)\s*$")

EXTERNAL_HEAD_RUNNING = "running"
EXTERNAL_HEAD_LANDED = "landed"
EXTERNAL_HEAD_RELEASED = "released"


class ControllerError(Exception):
    """A caller error (unknown repository, unknown setting, bad value); carries a stable `code`."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(detail or code)
        self.code = code
        self.detail = detail


class TaskError(ControllerError):
    """A task API error: unknown task or claim, expired or superseded claim, not authorized."""


class Controller:
    def __init__(
        self,
        store: Store,
        github: GitHubPort,
        clone: str,
        repository: str,
        *,
        revision: str,
        canonical_branch: str | None = None,
        clock: Callable[[], float] = time.time,
        rng: random.Random | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self.store = store
        self.github = github
        self.clone = clone
        self.repository = repository
        self.revision = revision
        self.clock = clock
        self.rng = rng or random.Random()
        self.log = logger or logging.getLogger("tauceti_queue.controller")
        self.started_at = clock()
        # `repository_admin(login)` per login, kept for `ADMIN_CACHE_SECONDS` and served stale while
        # GitHub cannot answer, so a quota outage neither refuses nor grants anything new.
        self.admins = StaleCache(
            github.repository_admin, ttl=ADMIN_CACHE_SECONDS, clock=clock, logger=self.log, what="admin"
        )
        self._lock = threading.RLock()
        self._wake = threading.Event()
        self._reasons: list[str] = []
        # The last status document and the monotonic time it was taken, served by `status()` when the
        # lock is held by a long tick.
        self._status_snapshot: tuple[float, dict[str, Any]] | None = None
        self.canonical_branch = canonical_branch or self._discover_canonical_branch()
        self._canonical: str | None = self.store.get_setting("state:canonical_sha")

    # --- settings ----------------------------------------------------------------------------------

    def setting(self, key: str) -> Any:
        if key not in DEFAULT_SETTINGS:
            raise ControllerError("unknown-setting", key)
        return self.store.get_setting(key, DEFAULT_SETTINGS[key])

    def effective_settings(self) -> dict[str, Any]:
        return {key: self.setting(key) for key in DEFAULT_SETTINGS}

    def set_setting(self, key: str, value: Any, actor: str) -> dict[str, Any]:
        """Change a runtime setting; applies to the next scheduling decision. Returns the effective settings."""
        with self._lock:
            if key not in DEFAULT_SETTINGS:
                raise ControllerError("unknown-setting", key)
            expected = DEFAULT_SETTINGS[key]
            if isinstance(expected, bool):
                if not isinstance(value, bool):
                    raise ControllerError("bad-setting-value", f"{key} must be a boolean")
            elif isinstance(expected, int):
                if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                    raise ControllerError("bad-setting-value", f"{key} must be a non-negative integer")
            elif not isinstance(value, type(expected)):
                raise ControllerError("bad-setting-value", f"{key} must be a {type(expected).__name__}")
            now = self.clock()
            previous = self.setting(key)
            with self.store.transaction():
                self.store.set_setting(key, value, actor, now)
                self.store.journal(now, "setting", key=key, value=value, actor=actor)
                if key == "publication_enabled" and value and not previous:
                    self._republish_retained_refs(now)
            self._wake.set()
            self._refresh_snapshot()
            return self.effective_settings()

    def _state(self, key: str, default: Any = None) -> Any:
        return self.store.get_setting(f"state:{key}", default)

    def _set_state(self, key: str, value: Any) -> None:
        self.store.set_setting(f"state:{key}", value, "controller", self.clock())

    def _count(self, counter: str, by: int = 1) -> None:
        counters = self._state("counters", {})
        counters[counter] = counters.get(counter, 0) + by
        self._set_state("counters", counters)

    # --- git helpers -------------------------------------------------------------------------------

    def _discover_canonical_branch(self) -> str:
        result = chain._git(self.clone, "symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD")
        if result.returncode == 0 and result.stdout.strip().startswith("origin/"):
            return result.stdout.strip()[len("origin/") :]
        return "main"

    def _canonical_ref(self) -> str:
        return f"refs/remotes/origin/{self.canonical_branch}"

    def _fetch_canonical(self) -> str:
        self.github.fetch_refs(self.repository, [f"+refs/heads/{self.canonical_branch}:{self._canonical_ref()}"])
        return chain.resolve_commit(self.clone, self._canonical_ref())

    def _retain_local(self, ref: str, sha: str) -> None:
        chain.git(self.clone, "update-ref", ref, sha)

    def _retain(self, ref: str, sha: str) -> None:
        """Retain `sha` under `ref` locally and, when publishing, on GitHub through the outbox."""
        self._retain_local(ref, sha)
        if self.setting("publication_enabled"):
            self._enqueue("retain_ref", f"retain:{ref}:{sha}", {"ref": ref, "sha": sha})

    def _local_ref(self, ref: str) -> str | None:
        """The SHA `ref` names in the clone, or None when the clone has no such ref."""
        result = chain._git(self.clone, "rev-parse", "--verify", "--quiet", ref)
        if result.returncode != 0:
            return None
        return result.stdout.strip() or None

    def _live_batches(self) -> list[dict[str, Any]]:
        """The head and open batches: the ones whose retained refs a task or CI may fetch from GitHub."""
        rows = self.store.rows("batches", {"repository": self.repository, "state": BATCH_HEAD}, order="id")
        return rows + self._open_batches()

    def _republish_retained_refs(self, now: float) -> int:
        """Enqueue the live batches' retained refs that never reached GitHub; returns how many.

        `_retain` pushes through the outbox only while `publication_enabled` is true, so a ref retained
        in shadow mode exists in the clone alone, and a repair task or CI fetching it from GitHub finds
        nothing. For the head and every open batch, the refs the controller would have pushed (the
        batch's tip under its current version and each non-removed member's accepted head) are
        enqueued when the clone still holds them under that name and the outbox has no entry for the
        ref and SHA. Done entries stay in the outbox, so the check is one indexed lookup per ref and no
        GitHub call: a pass over a fully published queue enqueues nothing. Runs at restart, when
        publication is switched on and once per tick.
        """
        if not self.setting("publication_enabled"):
            return 0
        enqueued = 0
        for batch in self._live_batches():
            wanted = [(self.batch_ref(batch["id"], batch["version"]), batch["tip"])]
            for member in self._members(batch["id"]):
                wanted.append((self.input_ref(member["number"], member["accepted_head"]), member["accepted_head"]))
            for ref, sha in wanted:
                key = f"retain:{ref}:{sha}"
                if self.store.row("outbox", {"key": key}) is not None or self._local_ref(ref) != sha:
                    continue
                self._enqueue("retain_ref", key, {"ref": ref, "sha": sha})
                enqueued += 1
        if enqueued:
            self.store.journal(now, "republish", enqueued=enqueued)
            self._wake.set()
        return enqueued

    @staticmethod
    def batch_ref(batch_id: int, version: int) -> str:
        return f"{chain.REF_NAMESPACE}/batches/{batch_id}/v{version}"

    @staticmethod
    def input_ref(number: int, sha: str) -> str:
        return f"{chain.REF_NAMESPACE}/inputs/{number}/{sha}"

    @staticmethod
    def staging_candidate_ref(candidate_id: int, generation: int) -> str:
        """The branch carrying a CI attempt's head, named as Bors names its train candidates.

        `pr-build.yml` resolves the dispatch through the observation feed's `train` and requires
        `staging_ref` to be the branch `staging-candidate-<candidate_id>-<generation>` whose tip is the
        dispatched head (TauCetiTools `bors_train_identity.py`, `ci_git.py`); the candidate id is the
        attempt's id and the generation the batch version, as the candidates feed reports them.
        """
        return f"staging-candidate-{candidate_id}-{generation}"

    @staticmethod
    def submission_ref(task_id: int, claim_id: int) -> str:
        return f"{chain.REF_NAMESPACE}/submissions/{task_id}/{claim_id}"

    def _fetch_head(self, number: int, sha: str) -> bool:
        """Bring a PR head into the clone: by its pull ref first, by SHA if the ref has moved on."""
        if chain.object_exists(self.clone, sha):
            return True
        probe = f"{chain.REF_NAMESPACE}/pulls/{number}"
        try:
            self.github.fetch_refs(self.repository, [f"+refs/pull/{number}/head:{probe}"])
        except Exception as error:  # noqa: BLE001 - the adapter's errors are reported, not propagated
            self.log.warning("fetch of refs/pull/%s/head failed: %s", number, error)
        if chain.object_exists(self.clone, sha):
            return True
        try:
            self.github.fetch_refs(self.repository, [sha])
        except Exception as error:  # noqa: BLE001
            self.log.warning("fetch of %s failed: %s", sha, error)
        return chain.object_exists(self.clone, sha)

    def _check_repo(self, repo: str) -> None:
        if repo != self.repository:
            raise ControllerError("unknown-repository", repo)

    # --- approvals ---------------------------------------------------------------------------------

    def approve(
        self,
        repo: str,
        pr: int,
        head_sha: str,
        reviewer: str,
        priority: int = 0,
        evidence: dict[str, Any] | None = None,
        *,
        approved_at: float | None = None,
        pull: Any = None,
    ) -> dict[str, Any]:
        """Record an approved input for `head_sha`. Idempotent per (PR, head); a member PR is not re-approved.

        `approved_at` lets an import (the cutover) keep an input's original admission age; `pull` is
        the live PR when the caller has just read it (the import validates before approving, and
        must not pay for a second read). Returns `{"accepted": bool, "reason": ..., "input": row | None}`;
        besides `validate_pull`'s reasons, a PR carrying one of Bors's blocking labels is refused with
        `blocked-label: <labels>`.
        """
        with self._lock:
            self._check_repo(repo)
            now = self.clock()
            existing = self._input_for(pr)
            if existing is not None:
                if existing["head_sha"] == head_sha and existing["state"] in (INPUT_WAITING, INPUT_RESERVED):
                    changed = {}
                    if priority != existing["priority"]:
                        changed["priority"] = priority
                    if changed:
                        self.store.update("inputs", {"id": existing["id"]}, **changed)
                    return {"accepted": True, "reason": "already-approved", "input": self._input_for(pr)}
                if existing["state"] == INPUT_INSERTED:
                    return {"accepted": False, "reason": "member", "input": existing}
                # A different head while waiting: the previous approval no longer describes the source.
                self.store.update(
                    "inputs", {"id": existing["id"]}, state=INPUT_CANCELLED, reason="superseded-by-new-approval"
                )
            if pull is None or pull.head_sha != head_sha:
                pull, reason = self.validate_pull(repo, pr, head_sha)
                if reason is not None:
                    return {"accepted": False, "reason": reason, "input": None}
            blocked = self._blocking_labels(pull)
            if blocked:
                self.store.journal(now, "approval-refused", number=pr, head=head_sha, labels=blocked)
                return {"accepted": False, "reason": f"blocked-label: {', '.join(blocked)}", "input": None}
            if not self._fetch_head(pr, head_sha):
                return {"accepted": False, "reason": "head-unavailable", "input": None}
            ref = self.input_ref(pr, head_sha)
            with self.store.transaction():
                self._retain(ref, head_sha)
                input_id = self.store.insert(
                    "inputs",
                    repository=repo,
                    number=pr,
                    head_sha=head_sha,
                    title=pull.title,
                    description=pull.body or "",
                    author=pull.author_login,
                    coauthors=None,
                    evidence=dumps(evidence or {}),
                    reviewer=reviewer,
                    priority=priority,
                    state=INPUT_WAITING,
                    approved_at=approved_at if approved_at is not None else now,
                    retained_ref=ref,
                )
                self.store.journal(now, "approved", number=pr, head=head_sha, reviewer=reviewer, input_id=input_id)
            self._wake.set()
            self._refresh_snapshot()
            return {"accepted": True, "reason": "approved", "input": self.store.row("inputs", {"id": input_id})}

    def validate_pull(self, repo: str, pr: int, head_sha: str) -> tuple[Any, str | None]:
        """Read the live PR and say why `head_sha` cannot be approved: `(pull, None)` when it can.

        The reasons are the stable codes `approve` returns: `fetch-failed: ...`, `head-mismatch`
        (the live head moved on), `closed`, `draft`, `base-branch`.
        """
        self._check_repo(repo)
        try:
            pull = self.github.fetch_pr(repo, pr)
        except Exception as error:  # noqa: BLE001
            return None, f"fetch-failed: {error}"
        if pull.head_sha != head_sha:
            with self._lock:
                self.store.journal(self.clock(), "approval-refused", number=pr, head=head_sha, live=pull.head_sha)
            return pull, "head-mismatch"
        if pull.state != "open":
            return pull, "closed"
        if pull.draft:
            return pull, "draft"
        if pull.base_ref != self.canonical_branch:
            return pull, "base-branch"
        return pull, None

    @staticmethod
    def _blocking_labels(pull: Any) -> list[str]:
        """The labels on a live PR that Bors's `block_labels` refused to merge under (`eligibility.KEEP`).

        `stacked-pr` is among them: TauCetiTools' stack policy applies it and relies on the merge
        backend refusing the labelled PR, so an approval of one is refused with the labels as the reason.
        """
        labels = {str(label).lower() for label in (getattr(pull, "labels", None) or ())}
        return sorted(labels & BLOCKING_LABELS)

    def input_for(self, repo: str, pr: int) -> dict[str, Any] | None:
        """The PR's current input row (the newest that is not cancelled, else the newest), or None."""
        with self._lock:
            self._check_repo(repo)
            return self._input_for(pr)

    def note_approval(self, source: str) -> None:
        """Count where an approval was discovered (`webhook` or `poll`); status reports both."""
        with self._lock:
            self._count(f"approvals_{source}")

    def record_approval_poll(self, summary: dict[str, Any]) -> None:
        """The approval poller's latest pass, kept in state for status; its approvals and revocations are counted."""
        with self._lock:
            self._set_state("approval_poll", summary)
            if summary.get("revoked"):
                self._count("approvals_revoked_by_poll", int(summary["revoked"]))

    # --- Bors's state at cutover -------------------------------------------------------------------

    def import_bors_state(
        self,
        observation: dict[str, Any],
        candidates: Any = None,
        *,
        now: float | None = None,
        dry_run: bool = False,
        release_head: bool = False,
    ) -> dict[str, Any]:
        """Import Bors's approved PRs from its observation feed (`tauceti_queue.bors_import`)."""
        from . import bors_import

        # Not under the controller lock: the import reads one PR per member from GitHub (minutes for
        # Bors's production queue) and the loop and the status endpoint must keep running meanwhile;
        # every record change goes through the locking methods, and the external head is recorded
        # before any input is approved, so no head can seal ahead of it.
        return bors_import.run(
            self,
            observation,
            candidates,
            now=self.clock() if now is None else now,
            dry_run=dry_run,
            release_head=release_head,
        )

    def external_head(self) -> dict[str, Any] | None:
        """Bors's running head at the cutover, while its landing is awaited (`state:external_head`)."""
        with self._lock:
            return self._state("external_head")

    def record_external_head(self, head: dict[str, Any], now: float) -> None:
        with self._lock:
            record = dict(head, state=EXTERNAL_HEAD_RUNNING, recorded_at=now, landed_members=[])
            with self.store.transaction():
                self._set_state("external_head", record)
                self.store.journal(
                    now,
                    "external-head",
                    batch_id=record.get("batch_id"),
                    head=record.get("head_sha"),
                    members=[m["pr"] for m in record.get("members", [])],
                )
            self._wake.set()
            self._refresh_snapshot()

    def release_external_head(self, reason: str, now: float) -> dict[str, Any] | None:
        """Stop waiting for Bors's head (it landed, was cancelled, or the operator released it)."""
        with self._lock:
            record = self._state("external_head")
            if record is None or record["state"] != EXTERNAL_HEAD_RUNNING:
                return record
            record = dict(record, state=EXTERNAL_HEAD_RELEASED, released_at=now, release_reason=reason)
            with self.store.transaction():
                self._set_state("external_head", record)
                self.store.journal(now, "external-head-released", batch_id=record.get("batch_id"), reason=reason)
            self._wake.set()
            self._refresh_snapshot()
            return record

    def _external_member_landed(self, numbers: set[int], how: str, now: float) -> None:
        """Members of Bors's head seen landing: the head is landed once every member has."""
        record = self._state("external_head")
        if record is None or record["state"] != EXTERNAL_HEAD_RUNNING:
            return
        members = {int(m["pr"]) for m in record.get("members", [])}
        landed = set(record.get("landed_members", [])) | (numbers & members)
        if landed == set(record.get("landed_members", [])):
            return
        record = dict(record, landed_members=sorted(landed))
        if landed >= members:
            record.update(state=EXTERNAL_HEAD_LANDED, landed_at=now, landed_how=how)
        with self.store.transaction():
            self._set_state("external_head", record)
            self.store.journal(
                now,
                "external-head-progress",
                batch_id=record.get("batch_id"),
                landed=sorted(landed),
                pending=sorted(members - landed),
                how=how,
                state=record["state"],
            )
        self._wake.set()

    def _external_head_blocks_sealing(self) -> str | None:
        record = self._state("external_head")
        if record is None or record["state"] != EXTERNAL_HEAD_RUNNING:
            return None
        members = record.get("members", [])
        pending = len(members) - len(record.get("landed_members", []))
        return (
            f"Bors's head (batch {record.get('batch_id')}, {len(members)} members, {pending} not yet landed) "
            "has not landed; the first head seals after that canonical advance or a release"
        )

    def unapprove(self, repo: str, pr: int, actor: str = "") -> dict[str, Any]:
        """`bors r-`: a waiting input is withdrawn; a batch member is not (membership is fixed)."""
        return self._withdraw(repo, pr, "unapproved", actor)

    def cancel(self, repo: str, pr: int, actor: str = "") -> dict[str, Any]:
        """`bors cancel`: the same as `unapprove`; the head is never cancelled."""
        return self._withdraw(repo, pr, "cancelled", actor)

    def _withdraw(self, repo: str, pr: int, reason: str, actor: str) -> dict[str, Any]:
        with self._lock:
            self._check_repo(repo)
            now = self.clock()
            current = self._input_for(pr)
            if current is None or current["state"] in (INPUT_LANDED, INPUT_CANCELLED):
                return {"withdrawn": False, "reason": "not-waiting"}
            if current["state"] == INPUT_INSERTED:
                self.store.journal(now, "withdrawal-refused", number=pr, reason=reason, actor=actor)
                return {"withdrawn": False, "reason": "member"}
            with self.store.transaction():
                self.store.update("inputs", {"id": current["id"]}, state=INPUT_CANCELLED, reason=reason)
                self.store.journal(now, "withdrawn", number=pr, reason=reason, actor=actor)
            self._refresh_snapshot()
            return {"withdrawn": True, "reason": reason}

    def set_priority(self, repo: str, pr: int, priority: int) -> dict[str, Any]:
        with self._lock:
            self._check_repo(repo)
            current = self._input_for(pr)
            if current is None or current["state"] not in (INPUT_WAITING, INPUT_RESERVED):
                return {"changed": False, "reason": "not-waiting"}
            self.store.update("inputs", {"id": current["id"]}, priority=priority)
            self._refresh_snapshot()
            return {"changed": True, "priority": priority}

    def mark_urgent(self, repo: str, pr: int, actor: str) -> dict[str, Any]:
        """The operator's urgent lane: the PR is drawn next and its batch seals as soon as the head lands."""
        with self._lock:
            self._check_repo(repo)
            now = self.clock()
            current = self._input_for(pr)
            if current is None or current["state"] in (INPUT_LANDED, INPUT_CANCELLED):
                return {"urgent": False, "reason": "not-queued"}
            with self.store.transaction():
                self.store.update("inputs", {"id": current["id"]}, urgent=1)
                if current["state"] == INPUT_INSERTED and current["batch_id"] is not None:
                    self.store.update("batches", {"id": current["batch_id"]}, urgent=1)
                self.store.journal(now, "urgent", number=pr, actor=actor, state=current["state"])
            self._wake.set()
            self._refresh_snapshot()
            return {"urgent": True, "state": current["state"]}

    def _input_for(self, pr: int) -> dict[str, Any] | None:
        """The PR's current input: the newest row that is not cancelled, else the newest row."""
        rows = self.store.rows("inputs", {"repository": self.repository, "number": pr}, order="id DESC")
        for row in rows:
            if row["state"] != INPUT_CANCELLED:
                return row
        return rows[0] if rows else None

    # --- events ------------------------------------------------------------------------------------

    def handle_event(self, kind: str, payload: dict[str, Any]) -> None:
        """A GitHub webhook. Commands are parsed by the adapter; here events only refresh what they describe."""
        with self._lock:
            now = self.clock()
            if kind == "push":
                ref = payload.get("ref", "")
                if ref == f"refs/heads/{self.canonical_branch}":
                    self._set_state("canonical_stale", True)
                self._push_to_member_branch(payload, now)
            elif kind == "pull_request":
                self._pull_request_event(payload, now)
            elif kind in ("status", "check_run", "check_suite", "workflow_run"):
                self._mark_observation(kind, payload)
            # issue_comment and pull_request_review carry commands; the adapter has already parsed them.
            self.store.journal(now, "event", event=kind, action=payload.get("action", ""))
            self._wake.set()

    def _pull_request_event(self, payload: dict[str, Any], now: float) -> None:
        action = payload.get("action", "")
        pull = payload.get("pull_request", {}) or {}
        number = int(payload.get("number") or pull.get("number") or 0)
        if not number:
            return
        if action == "closed" and (pull.get("merged") or str(pull.get("title") or "").startswith(MERGED_TITLE_PREFIX)):
            self._external_member_landed({number}, "closed-by-bors", now)
        current = self._input_for(number)
        if current is None:
            return
        if current["state"] == INPUT_INSERTED:
            # Scenario 21: closing, relabelling, re-reviewing a member changes nothing; the content lands.
            self.store.journal(now, "member-event-ignored", number=number, action=action, batch_id=current["batch_id"])
            return
        if current["state"] not in (INPUT_WAITING, INPUT_RESERVED):
            return
        if action == "closed":
            reason = "merged-externally" if pull.get("merged") else "closed"
            self.store.update("inputs", {"id": current["id"]}, state=INPUT_CANCELLED, reason=reason)
            self.store.journal(now, "withdrawn", number=number, reason=reason)
        elif action == "synchronize":
            new_head = (pull.get("head") or {}).get("sha")
            if new_head and new_head != current["head_sha"]:
                self.store.update("inputs", {"id": current["id"]}, state=INPUT_CANCELLED, reason="head-changed")
                self.store.journal(now, "withdrawn", number=number, reason="head-changed", head=new_head)
        elif action == "converted_to_draft":
            self.store.update("inputs", {"id": current["id"]}, state=INPUT_CANCELLED, reason="draft")
            self.store.journal(now, "withdrawn", number=number, reason="draft")

    def _push_to_member_branch(self, payload: dict[str, Any], now: float) -> None:
        # A push to a member's branch during a candidate build never touches the build (scenario 19);
        # the accepted head is what the batch carries. Nothing to do but note it.
        ref = payload.get("ref", "")
        if ref.startswith("refs/heads/") and ref != f"refs/heads/{self.canonical_branch}":
            self.store.journal(now, "branch-push", ref=ref, after=payload.get("after", ""))

    def _mark_observation(self, kind: str, payload: dict[str, Any]) -> None:
        sha = payload.get("sha") or (payload.get("check_run") or {}).get("head_sha")
        sha = (
            sha
            or (payload.get("check_suite") or {}).get("head_sha")
            or (payload.get("workflow_run") or {}).get("head_sha")
        )
        head = self._head()
        if head is None:
            return
        attempt = self._current_attempt(head)
        if attempt is None:
            return
        if sha in (None, head["tip"], attempt["head_sha"]):
            self.store.update("ci_attempts", {"id": attempt["id"]}, needs_observation=1)

    # --- inference ---------------------------------------------------------------------------------

    def report_inference(self, worker_identity: str, ok: bool, error: str = "") -> dict[str, Any] | None:
        """A worker's report of a provider call: failures accumulate into a hold, a success lifts it."""
        with self._lock:
            now = self.clock()
            hold = self._state("inference_hold")
            if ok:
                self._set_state("inference_failures", 0)
                if hold:
                    self._set_state("inference_hold", None)
                    self._count("inference_holds_resolved")
                    self.store.journal(now, "inference-resumed", worker=worker_identity, since=hold["since"])
                    self._wake.set()
                return None
            failures = int(self._state("inference_failures", 0)) + 1
            self._set_state("inference_failures", failures)
            if hold is None:
                if failures >= int(self.setting("inference_failure_threshold")):
                    hold = {
                        "since": now,
                        "last_error": error,
                        "last_report_at": now,
                        "reports": failures,
                        "attempts": 0,
                        "next_retry_at": now + int(self.setting("inference_hold_backoff_seconds")),
                        "reporter": worker_identity,
                    }
                    self._set_state("inference_hold", hold)
                    self._count("inference_holds")
                    self.store.journal(now, "inference-hold", error=error, worker=worker_identity)
                return hold
            hold["last_error"] = error
            hold["last_report_at"] = now
            hold["reports"] = failures
            if now >= hold["next_retry_at"]:
                hold["attempts"] += 1
                backoff = min(
                    int(self.setting("inference_hold_backoff_seconds")) * (2 ** hold["attempts"]),
                    int(self.setting("inference_hold_backoff_max_seconds")),
                )
                hold["next_retry_at"] = now + backoff
            self._set_state("inference_hold", hold)
            return hold

    def inference_hold(self) -> dict[str, Any] | None:
        return self._state("inference_hold")

    # --- tasks -------------------------------------------------------------------------------------

    def _repository_admin(self, login: str) -> bool | None:
        """`repository_admin(login)` through the cache; None when GitHub cannot answer and never has."""
        try:
            return bool(self.admins.get(login, login))
        except GitHubUnavailable as error:
            self.log.warning("admin check for %s failed with nothing cached: %s", login, error)
            return None

    def claim_task(self, worker_identity: str, github_login: str) -> Task | None:
        """Hand the head's open task to an authorized worker under a renewable claim, within `repair_concurrency`."""
        with self._lock:
            now = self.clock()
            self._expire_claims(now)
            admin = self._repository_admin(github_login)
            if not admin:
                reason = "not-admin" if admin is False else "github-unavailable"
                self.store.journal(now, "claim-refused", worker=worker_identity, login=github_login, reason=reason)
                return None
            head = self._head()
            if head is None:
                return None
            task = self._open_task(head)
            if task is None:
                return None
            active = self._active_claims(head["id"], task_id=task["id"])
            if len(active) >= int(self.setting("repair_concurrency")):
                self._count("claim_collisions")
                self.store.journal(
                    now, "claim-refused", worker=worker_identity, reason="concurrency", task_id=task["id"]
                )
                return None
            token = secrets.token_hex(16)
            with self.store.transaction():
                claim_id = self.store.insert(
                    "claims",
                    batch_id=head["id"],
                    batch_version=head["version"],
                    operation=f"task:{task['kind']}",
                    worker=worker_identity,
                    github_login=github_login,
                    token=token,
                    starting_tip=head["tip"],
                    task_id=task["id"],
                    created_at=now,
                    expires_at=now + int(self.setting("claim_ttl_seconds")),
                )
                self.store.update("tasks", {"id": task["id"]}, claims=task["claims"] + 1)
                self.store.journal(
                    now, "claimed", head["id"], task_id=task["id"], worker=worker_identity, claim_id=claim_id
                )
            return self._task_record(task, head, claim_id, token, now + int(self.setting("claim_ttl_seconds")))

    def renew_task(self, task_id: int, claim_token: str) -> float:
        with self._lock:
            now = self.clock()
            claim = self._claim_for(task_id, claim_token, now)
            expires = now + int(self.setting("claim_ttl_seconds"))
            self.store.update("claims", {"id": claim["id"]}, expires_at=expires, renewed_at=now)
            return expires

    def release_task(self, task_id: int, claim_token: str, reason: str, error: str = "") -> None:
        """A worker gives a task back. Reason `inference_unavailable` counts toward the inference hold."""
        with self._lock:
            now = self.clock()
            claim = self._claim_for(task_id, claim_token, now)
            with self.store.transaction():
                self.store.update("claims", {"id": claim["id"]}, released_at=now, release_reason=reason)
                self.store.journal(now, "released", claim["batch_id"], task_id=task_id, reason=reason, error=error)
            if reason == "inference_unavailable":
                self.report_inference(claim["worker"], False, error)

    def submit_task(self, task_id: int, claim_token: str, tip_sha: str, worker_notes: str = "") -> Adoption:
        """Validate a proposed chain against the task and adopt it as the batch's next version.

        A late result (expired claim, superseded task or batch version) is rejected but its commits
        stay retained under a submissions ref. See `Adoption` for the reason codes.
        """
        with self._lock:
            now = self.clock()
            task = self.store.row("tasks", {"id": task_id})
            if task is None:
                raise TaskError("unknown-task", str(task_id))
            claim = self.store.row("claims", {"task_id": task_id, "token": claim_token})
            if claim is None:
                raise TaskError("unknown-claim", "no claim with that token on this task")
            batch = self.store.row("batches", {"id": task["batch_id"]})
            assert batch is not None
            retained = self._retain_submission(task, claim, tip_sha)

            def reject(reason: str, violations: list[chain.Violation] | None = None) -> Adoption:
                self.store.insert(
                    "submissions",
                    task_id=task_id,
                    claim_id=claim["id"],
                    tip=tip_sha,
                    retained_ref=retained or "",
                    accepted=0,
                    reason=reason,
                    worker_notes=worker_notes,
                    created_at=now,
                )
                self.store.update("tasks", {"id": task_id}, submissions=task["submissions"] + 1)
                self.store.journal(now, "submission-rejected", batch["id"], task_id=task_id, reason=reason, tip=tip_sha)
                return Adoption(
                    False,
                    reason,
                    task_id,
                    batch["id"],
                    batch["version"],
                    tip_sha,
                    retained,
                    [v.to_dict() for v in violations or []],
                )

            if claim["released_at"] is not None:
                return reject("claim-expired" if claim["release_reason"] == "expired" else "claim-superseded")
            if claim["expires_at"] <= now:
                self.store.update("claims", {"id": claim["id"]}, released_at=now, release_reason="expired")
                return reject("claim-expired")
            if not self._repository_admin(claim["github_login"]):
                return reject("not-authorized")
            if task["state"] != TASK_OPEN or batch["version"] != task["batch_version"] or batch["state"] != BATCH_HEAD:
                return reject("version-mismatch")
            if retained is None:
                return reject("tip-unavailable")
            members = self._members(batch["id"])
            records = [self._chain_member(member) for member in members]
            report = chain.validate(self.clone, task["onto"], tip_sha, records)
            if not report.ok:
                self._count("chain_validation_failures")
                self.store.journal(
                    now,
                    "chain-invalid",
                    batch["id"],
                    task_id=task_id,
                    violations=[v.to_dict() for v in report.violations],
                )
                return reject("invalid-chain", report.violations)
            comparison = chain.compare(self.clone, task["base"], task["tip"], task["onto"], tip_sha)
            if not comparison.ok:
                self._count("chain_validation_failures")
                self.store.journal(
                    now,
                    "chain-invalid",
                    batch["id"],
                    task_id=task_id,
                    violations=[v.to_dict() for v in comparison.violations],
                )
                return reject("invalid-chain", comparison.violations)
            unexplained = [m for m in comparison.members if m.provenance == chain.EDITED and not m.explanation.strip()]
            if unexplained:
                violations = [
                    chain.Violation(
                        "explanation-required",
                        f"{m.source_pr}: content changed but no explanation was appended to the message",
                        commit=m.new_sha,
                        source_pr=m.source_pr,
                    )
                    for m in unexplained
                ]
                return reject("explanation-required", violations)
            return self._adopt(task, claim, batch, tip_sha, comparison, report, worker_notes, retained, now)

    def _retain_submission(self, task: dict[str, Any], claim: dict[str, Any], tip_sha: str) -> str | None:
        ref = self.submission_ref(task["id"], claim["id"])
        if not chain.object_exists(self.clone, tip_sha):
            try:
                self.github.fetch_refs(self.repository, [f"+{ref}:{ref}"])
            except Exception as error:  # noqa: BLE001
                self.log.warning("fetch of %s failed: %s", ref, error)
            if not chain.object_exists(self.clone, tip_sha):
                try:
                    self.github.fetch_refs(self.repository, [tip_sha])
                except Exception as error:  # noqa: BLE001
                    self.log.warning("fetch of %s failed: %s", tip_sha, error)
        if not chain.object_exists(self.clone, tip_sha):
            return None
        retained = f"{chain.REF_NAMESPACE}/batches/{task['batch_id']}/submissions/{tip_sha}"
        self._retain(retained, tip_sha)
        return retained

    def _adopt(
        self,
        task: dict[str, Any],
        claim: dict[str, Any],
        batch: dict[str, Any],
        tip_sha: str,
        comparison: chain.ComparisonReport,
        report: chain.ValidationReport,
        worker_notes: str,
        retained: str,
        now: float,
    ) -> Adoption:
        version = batch["version"] + 1
        by_pr = {record.source_pr: record for record in report.commits}
        provenance: dict[str, str] = {}
        with self.store.transaction():
            for member in self._members(batch["id"]):
                outcome = next((m for m in comparison.members if m.source_pr == member["source_pr"]), None)
                record = by_pr.get(member["source_pr"])
                new_sha = record.sha if record else (outcome.new_sha if outcome else None)
                values: dict[str, Any] = {"commit_sha": new_sha}
                if outcome is not None and outcome.provenance == chain.EDITED:
                    explanation = (member["explanation"] + "\n\n" + outcome.explanation).strip()
                    edited = loads(member["edited_shas"], []) + [new_sha]
                    values.update(
                        provenance="edited", task_id=task["id"], explanation=explanation, edited_shas=dumps(edited)
                    )
                    if record is not None and record.empty:
                        self._count("shrink_to_empty")
                self.store.update("members", {"id": member["id"]}, **values)
                provenance[member["source_pr"]] = values.get("provenance", member["provenance"])
            self.store.update(
                "batches",
                {"id": batch["id"]},
                version=version,
                base=task["onto"],
                tip=tip_sha,
            )
            self.store.update(
                "tasks",
                {"id": task["id"]},
                state=TASK_SUBMITTED,
                resolved_at=now,
                resolution="adopted",
                submissions=task["submissions"] + 1,
            )
            self.store.update("claims", {"id": claim["id"]}, released_at=now, release_reason="submitted")
            for other in self._active_claims(batch["id"]):
                if other["id"] != claim["id"]:
                    self.store.update("claims", {"id": other["id"]}, released_at=now, release_reason="superseded")
            for other_task in self.store.rows("tasks", {"batch_id": batch["id"], "state": TASK_OPEN}):
                self.store.update(
                    "tasks",
                    {"id": other_task["id"]},
                    state=TASK_SUPERSEDED,
                    resolved_at=now,
                    resolution="adopted-other",
                )
            self.store.insert(
                "submissions",
                task_id=task["id"],
                claim_id=claim["id"],
                tip=tip_sha,
                retained_ref=self.batch_ref(batch["id"], version),
                accepted=1,
                reason="adopted",
                worker_notes=worker_notes,
                created_at=now,
            )
            self._retain(self.batch_ref(batch["id"], version), tip_sha)
            self._count("repair_rounds" if task["kind"] == "repair" else "rebase_conflicts_resolved")
            self.store.journal(
                now, "adopted", batch["id"], task_id=task["id"], version=version, tip=tip_sha, provenance=provenance
            )
        self.report_inference(claim["worker"], True)
        self._wake.set()
        return Adoption(
            True,
            "adopted",
            task["id"],
            batch["id"],
            version,
            tip_sha,
            self.batch_ref(batch["id"], version),
            provenance=provenance,
        )

    def _claim_for(self, task_id: int, token: str, now: float) -> dict[str, Any]:
        claim = self.store.row("claims", {"task_id": task_id, "token": token})
        if claim is None:
            raise TaskError("unknown-claim", "no claim with that token on this task")
        if claim["released_at"] is not None:
            raise TaskError("claim-expired" if claim["release_reason"] == "expired" else "claim-superseded")
        if claim["expires_at"] <= now:
            self.store.update("claims", {"id": claim["id"]}, released_at=now, release_reason="expired")
            raise TaskError("claim-expired")
        return claim

    def _open_task(self, head: dict[str, Any]) -> dict[str, Any] | None:
        return self.store.row(
            "tasks", {"batch_id": head["id"], "batch_version": head["version"], "state": TASK_OPEN}, order="id DESC"
        )

    def _task_record(
        self, task: dict[str, Any], head: dict[str, Any], claim_id: int, token: str, expires: float
    ) -> Task:
        members = []
        for member in self._members(head["id"]):
            input_row = self.store.row("inputs", {"id": member["input_id"]}) or {}
            members.append(
                TaskMember(
                    source_pr=member["source_pr"],
                    number=member["number"],
                    accepted_head=member["accepted_head"],
                    commit=member["commit_sha"],
                    provenance=member["provenance"],
                    title=input_row.get("title", ""),
                    description=input_row.get("description", ""),
                    author=input_row.get("author", ""),
                    explanation=member["explanation"],
                    retained_ref=self.input_ref(member["number"], member["accepted_head"]),
                )
            )
        refs = [self.batch_ref(head["id"], head["version"])] + [m.retained_ref for m in members]
        return Task(
            task_id=task["id"],
            kind=task["kind"],
            repository=self.repository,
            batch_id=head["id"],
            batch_version=head["version"],
            base=task["base"],
            tip=task["tip"],
            onto=task["onto"],
            members=members,
            diagnostics=loads(task["diagnostics"], []),
            retained_refs=refs,
            submission_ref=self.submission_ref(task["id"], claim_id),
            claim_token=token,
            expires_at=expires,
            instruction=AGENT_INSTRUCTION,
            created_at=task["created_at"],
            extra={"canonical_branch": self.canonical_branch, "claim_id": claim_id},
        )

    def _create_task(
        self,
        head: dict[str, Any],
        kind: str,
        onto: str,
        diagnostics: list[dict[str, Any]],
        now: float,
        ci_attempt_id: int | None = None,
    ) -> int:
        with self.store.transaction():
            for stale in self.store.rows("tasks", {"batch_id": head["id"], "state": TASK_OPEN}):
                self.store.update(
                    "tasks", {"id": stale["id"]}, state=TASK_SUPERSEDED, resolved_at=now, resolution="replaced"
                )
            task_id = self.store.insert(
                "tasks",
                kind=kind,
                repository=self.repository,
                batch_id=head["id"],
                batch_version=head["version"],
                base=head["base"],
                tip=head["tip"],
                onto=onto,
                diagnostics=dumps(diagnostics),
                state=TASK_OPEN,
                created_at=now,
                ci_attempt_id=ci_attempt_id,
            )
            self._count(f"tasks_{kind}")
            self.store.journal(now, "task", head["id"], task_id=task_id, task_kind=kind, onto=onto)
        return task_id

    # --- claims ------------------------------------------------------------------------------------

    def _active_claims(self, batch_id: int, task_id: int | None = None) -> list[dict[str, Any]]:
        now = self.clock()
        where: dict[str, Any] = {"batch_id": batch_id, "released_at": None}
        if task_id is not None:
            where["task_id"] = task_id
        return [row for row in self.store.rows("claims", where) if row["expires_at"] > now]

    def _expire_claims(self, now: float) -> None:
        for claim in self.store.rows("claims", {"released_at": None}):
            if claim["expires_at"] <= now:
                with self.store.transaction():
                    self.store.update("claims", {"id": claim["id"]}, released_at=now, release_reason="expired")
                    self.store.journal(
                        now, "claim-expired", claim["batch_id"], claim_id=claim["id"], worker=claim["worker"]
                    )

    def _take_claim(self, batch: dict[str, Any], operation: str, now: float) -> dict[str, Any]:
        claim_id = self.store.insert(
            "claims",
            batch_id=batch["id"],
            batch_version=batch["version"],
            operation=operation,
            worker="controller",
            token=secrets.token_hex(8),
            starting_tip=batch["tip"],
            created_at=now,
            expires_at=now + int(self.setting("claim_ttl_seconds")),
        )
        return self.store.row("claims", {"id": claim_id}) or {}

    def _release_claim(self, claim: dict[str, Any], reason: str, now: float) -> None:
        self.store.update("claims", {"id": claim["id"]}, released_at=now, release_reason=reason)

    # --- records -----------------------------------------------------------------------------------

    def _open_batches(self) -> list[dict[str, Any]]:
        """Open batches oldest first: insertion tries them in this order and sealing takes the first."""
        return self.store.rows("batches", {"repository": self.repository, "state": BATCH_OPEN}, order="id")

    def _open_batch(self) -> dict[str, Any] | None:
        batches = self._open_batches()
        return batches[0] if batches else None

    def _head(self) -> dict[str, Any] | None:
        return self.store.row("batches", {"repository": self.repository, "state": BATCH_HEAD}, order="id DESC")

    def _members(self, batch_id: int) -> list[dict[str, Any]]:
        return self.store.rows("members", {"batch_id": batch_id, "removed": 0}, order="position")

    def _chain_member(self, member: dict[str, Any]) -> chain.Member:
        input_row = self.store.row("inputs", {"id": member["input_id"]}) or {}
        coauthors = loads(input_row.get("coauthors"))
        return chain.Member(
            self.repository,
            member["number"],
            member["accepted_head"],
            title=input_row.get("title", ""),
            description=input_row.get("description", ""),
            author=input_row.get("author", ""),
            coauthors=None if coauthors is None else tuple(coauthors),
        )

    def _current_attempt(self, head: dict[str, Any]) -> dict[str, Any] | None:
        return self.store.row(
            "ci_attempts", {"batch_id": head["id"], "batch_version": head["version"]}, order="id DESC"
        )

    def _create_open_batch(self, base: str, now: float) -> dict[str, Any]:
        batch_id = self.store.insert(
            "batches",
            repository=self.repository,
            version=0,
            base=base,
            tip=base,
            sealed=0,
            state=BATCH_OPEN,
            created_at=now,
        )
        self.store.journal(now, "batch-opened", batch_id, base=base)
        return self.store.row("batches", {"id": batch_id}) or {}

    # --- the reconciliation pass -------------------------------------------------------------------

    def tick(self) -> list[str]:
        """One reconciliation pass; returns the reasons the queue is waiting (also reported in status).

        The pass ends by taking the status snapshot `status()` serves while a later pass holds the lock.
        """
        with self._lock:
            try:
                return self._tick()
            finally:
                self._refresh_snapshot()

    def _tick(self) -> list[str]:
        self._reasons = []
        now = self.clock()
        self._expire_claims(now)
        self._drain_outbox(now)
        try:
            canonical = self._fetch_canonical()
        except Exception as error:  # noqa: BLE001 - the pass reports the failure and waits for the next one
            self._reasons.append(f"canonical branch unavailable: {error}")
            return list(self._reasons)
        self._observe_canonical(canonical, now)
        hold = self._state("inference_hold")
        if hold:
            self._reasons.append(f"inference hold since {hold['since']:.0f}: {hold['last_error']}")
        self._insertion_passes(now)
        self._seal(now)
        self._reconcile_head(now)
        self._republish_retained_refs(now)
        self._drain_outbox(self.clock())
        self._set_state("last_tick_at", now)
        return list(self._reasons)

    def recover(self) -> dict[str, Any]:
        """Restart reconciliation: release in-process claims, return reservations, leave worker claims and the
        outbox, and enqueue the retained refs of live batches that never reached GitHub."""
        with self._lock:
            now = self.clock()
            released = 0
            returned = 0
            with self.store.transaction():
                for claim in self.store.rows("claims", {"released_at": None, "worker": "controller"}):
                    self.store.update("claims", {"id": claim["id"]}, released_at=now, release_reason="restart")
                    released += 1
                for row in self.store.rows("inputs", {"repository": self.repository, "state": INPUT_RESERVED}):
                    self.store.update("inputs", {"id": row["id"]}, state=INPUT_WAITING)
                    returned += 1
                self._attribute_legacy_conflicts()
                self._expire_claims(now)
                republished = self._republish_retained_refs(now)
                pending = self.store.count("outbox", {"state": "pending"})
                self.store.journal(
                    now,
                    "restart",
                    released=released,
                    returned=returned,
                    outbox_pending=pending,
                    republished=republished,
                    revision=self.revision,
                )
            self.started_at = now
            self._refresh_snapshot()
            return {
                "released_claims": released,
                "returned_reservations": returned,
                "outbox_pending": pending,
                "republished_refs": republished,
            }

    def _attribute_legacy_conflicts(self) -> None:
        """Databases from before `probe_conflicts` recorded a conflict by base only, against the one open batch."""
        open_batches = self._open_batches()
        if len(open_batches) != 1:
            return
        batch = open_batches[0]
        for row in self.store.rows("inputs", {"repository": self.repository, "state": INPUT_WAITING}):
            if row["last_probe_base"] == batch["base"] and not loads(row["probe_conflicts"], {}):
                self.store.update("inputs", {"id": row["id"]}, probe_conflicts=dumps({str(batch["id"]): batch["base"]}))

    def wake_event(self) -> threading.Event:
        return self._wake

    # --- canonical advances ------------------------------------------------------------------------

    def _observe_canonical(self, canonical: str, now: float) -> None:
        previous = self._canonical
        self._canonical = canonical
        self._set_state("canonical_stale", False)
        if canonical == previous:
            return
        self._set_state("canonical_sha", canonical)
        self.store.journal(now, "canonical-advance", previous=previous, current=canonical)
        head = self._head()
        if head is not None and chain.is_ancestor(self.clone, head["tip"], canonical):
            how = "promoted" if head["expected_parent"] else "external"
            self._landed(head, how, now)
        if previous and chain.is_ancestor(self.clone, previous, canonical):
            landed_prs: set[int] = set()
            bors_landed: set[int] = set()
            try:
                for commit in chain.chain_commits(self.clone, previous, canonical):
                    source = commit.source_pr
                    if source and source.startswith(f"{self.repository}#"):
                        landed_prs.add(int(source.rsplit("#", 1)[1]))
                    elif not source:
                        # Bors's squash commits carry no Source-PR trailer; the subject names the PR.
                        lines = (commit.message or "").splitlines()
                        match = _BORS_SUBJECT_RE.search(lines[0]) if lines else None
                        if match:
                            bors_landed.add(int(match["number"]))
            except chain.ChainError:
                landed_prs = set()
            for number in sorted(landed_prs | bors_landed):
                row = self._input_for(number)
                if row is not None and row["state"] in (INPUT_WAITING, INPUT_RESERVED):
                    self.store.update("inputs", {"id": row["id"]}, state=INPUT_CANCELLED, reason="landed-externally")
                    self.store.journal(now, "withdrawn", number=number, reason="landed-externally")
            if bors_landed:
                self._external_member_landed(bors_landed, "canonical-advance", now)
        external = self._state("external_head")
        if (
            external is not None
            and external["state"] == EXTERNAL_HEAD_RUNNING
            and external.get("head_sha")
            and chain.object_exists(self.clone, external["head_sha"])
            and chain.is_ancestor(self.clone, external["head_sha"], canonical)
        ):
            self._external_member_landed({int(m["pr"]) for m in external.get("members", [])}, "head-ancestor", now)

    # --- insertion ---------------------------------------------------------------------------------

    def _waiting(self) -> list[dict[str, Any]]:
        rows = self.store.rows("inputs", {"repository": self.repository, "state": INPUT_WAITING})
        rows.sort(key=lambda row: (-row["urgent"], -row["priority"], row["approved_at"], row["id"]))
        return rows

    def _insertion_passes(self, now: float) -> None:
        """Probe each waiting input at most once per open batch per tick, one age-windowed random draw per pass.

        Open batches are tried oldest first. An input that conflicted with this very tip would conflict
        again, and a textual conflict is with content already in the chain, so appending members cannot
        resolve it: an input whose recorded conflict is against a batch's current base is not probed there
        again but against the next open batch, created on the canonical commit (never on another batch's
        tip) while fewer than `open_batch_limit` are open. An input that conflicts with every open batch
        waits for a base to change (a new open batch or a rebase).

        The passes stop once `insertion_probe_budget_seconds` of wall time is spent (each probe is a git
        replay onto a batch's tip, and the controller lock is held throughout the tick); the remaining
        inputs wait for the next tick, which the loop is woken to run at once. `probed` is per tick and
        the draw's age bound carries over in `passes_waited`, so a paused tick starves nobody.
        """
        probed: set[tuple[int, int]] = set()  # (input, batch) pairs probed in this tick
        started = time.monotonic()
        budget = float(self.setting("insertion_probe_budget_seconds"))
        while self._waiting():
            if probed and time.monotonic() - started >= budget:
                self._reasons.append(f"insertion paused after {len(probed)} probes (budget)")
                self._wake.set()
                break
            batches = self._ensure_open_batches(now)
            if batches is None:
                break
            remaining = self._waiting()
            target: dict[str, Any] | None = None
            eligible: list[dict[str, Any]] = []
            for batch in batches:
                eligible = [
                    row
                    for row in remaining
                    if (row["id"], batch["id"]) not in probed and self._probe_eligible(row, batch)
                ]
                if eligible:
                    target = batch
                    break
                remaining = [row for row in remaining if self._conflicted_with(row, batch)]
                if not remaining:
                    break
            if target is None:
                if not remaining or len(batches) >= max(1, int(self.setting("open_batch_limit"))):
                    break
                assert self._canonical is not None
                target = self._create_open_batch(self._canonical, now)
                eligible = remaining
            window = eligible[: max(1, int(self.setting("insertion_age_window")))]
            chosen = self._draw(eligible)
            probed.add((chosen["id"], target["id"]))
            with self.store.transaction():
                for row in window:
                    if row["id"] != chosen["id"]:
                        self.store.update("inputs", {"id": row["id"]}, passes_waited=row["passes_waited"] + 1)
                        self._set_state(
                            "max_passes_waited", max(int(self._state("max_passes_waited", 0)), row["passes_waited"] + 1)
                        )
                self.store.update("inputs", {"id": chosen["id"]}, passes_waited=0)
                self._set_state("insertion_passes", int(self._state("insertion_passes", 0)) + 1)
            self._probe(chosen, target, now)
        self._remove_idle_open_batches(now)

    @staticmethod
    def _conflicted_with(row: dict[str, Any], batch: dict[str, Any]) -> bool:
        """The input's recorded conflict against this batch is against the batch's current base."""
        return loads(row["probe_conflicts"], {}).get(str(batch["id"])) == batch["base"]

    def _probe_eligible(self, row: dict[str, Any], batch: dict[str, Any]) -> bool:
        return row["last_probe_tip"] != batch["tip"] and not self._conflicted_with(row, batch)

    def _draw(self, waiting: list[dict[str, Any]]) -> dict[str, Any]:
        window = waiting[: max(1, int(self.setting("insertion_age_window")))]
        urgent = [row for row in window if row["urgent"]]
        if urgent:
            return urgent[0]
        limit = int(self.setting("insertion_age_window"))
        overdue = [row for row in window if row["passes_waited"] >= limit]
        if overdue:
            return overdue[0]  # the age bound: nobody waits more than the window's passes without a draw
        return self.rng.choice(window)

    def _ensure_open_batches(self, now: float) -> list[dict[str, Any]] | None:
        """The open batches oldest first, stale empty ones removed and the first created when none remains."""
        canonical = self._canonical
        if canonical is None:
            self._reasons.append("canonical branch not yet observed")
            return None
        batches = []
        for batch in self._open_batches():
            if not self._members(batch["id"]) and batch["base"] != canonical:
                self._remove_batch(batch, "stale-empty", now)
            else:
                batches.append(batch)
        if not batches:
            batches.append(self._create_open_batch(canonical, now))
        return batches

    def _remove_idle_open_batches(self, now: float) -> None:
        """Remove empty open batches: a later one as soon as the pass leaves it empty (it is created on
        demand for an input that conflicts with the batches before it), the first only when nothing waits
        and its base is stale."""
        for position, batch in enumerate(self._open_batches()):
            if self._members(batch["id"]):
                continue
            if position > 0:
                self._remove_batch(batch, "idle", now)
            elif not self._waiting() and self._canonical is not None and batch["base"] != self._canonical:
                self._remove_batch(batch, "idle", now)

    def _remove_batch(self, batch: dict[str, Any], reason: str, now: float) -> None:
        with self.store.transaction():
            self.store.update("batches", {"id": batch["id"]}, state=BATCH_REMOVED)
            self.store.journal(now, "batch-removed", batch["id"], reason=reason)

    def _probe(self, row: dict[str, Any], batch: dict[str, Any], now: float) -> bool:
        """Reserve the input, claim the open batch, replay the squash onto its tip; commit or roll back."""
        if self._active_claims(batch["id"]):
            self._reasons.append(f"open batch {batch['id']} is claimed")
            return False
        with self.store.transaction():
            self.store.update(
                "inputs", {"id": row["id"]}, state=INPUT_RESERVED, batch_id=batch["id"], probes=row["probes"] + 1
            )
            claim = self._take_claim(batch, "insert", now)
        member = chain.Member(
            self.repository,
            row["number"],
            row["head_sha"],
            title=row["title"],
            description=row["description"],
            author=row["author"],
            coauthors=None if loads(row["coauthors"]) is None else tuple(loads(row["coauthors"])),
        )
        chain_id = f"probe-{batch['id']}-{secrets.token_hex(4)}"
        try:
            if not self._fetch_head(row["number"], row["head_sha"]):
                raise chain.ChainError(f"accepted head {row['head_sha']} is not available")
            result = chain.build(self.clone, batch["tip"], [member], chain_id=chain_id)
        except chain.ChainError as error:
            with self.store.transaction():
                self.store.update(
                    "inputs",
                    {"id": row["id"]},
                    state=INPUT_WAITING,
                    batch_id=None,
                    last_probe=f"error: {error}",
                    last_probe_base="",
                )
                self._release_claim(claim, "probe-error", now)
                self.store.journal(now, "probe-error", batch["id"], number=row["number"], error=str(error))
            return False
        finally:
            chain.delete_chain_ref(self.clone, chain_id)
        current = self.store.row("batches", {"id": batch["id"]})
        if current is None or current["version"] != batch["version"] or current["state"] != BATCH_OPEN:
            # The batch moved under us (it cannot while we hold the claim, but recheck as the plan demands).
            with self.store.transaction():
                self.store.update(
                    "inputs", {"id": row["id"]}, state=INPUT_WAITING, batch_id=None, last_probe="batch-moved"
                )
                self._release_claim(claim, "batch-moved", now)
            return False
        if result.status != chain.CLEAN:
            conflict = result.conflict
            paths = list(conflict.conflict_paths) if conflict else []
            against_main = batch["tip"] == batch["base"] == self._canonical
            with self.store.transaction():
                if against_main:
                    self.store.update(
                        "inputs",
                        {"id": row["id"]},
                        state=INPUT_CANCELLED,
                        batch_id=None,
                        reason="conflicts-with-main",
                        last_probe=dumps(paths),
                    )
                    self._conflict_request(row, paths, now)
                else:
                    open_ids = {str(b["id"]) for b in self._open_batches()}
                    conflicts = {k: v for k, v in loads(row["probe_conflicts"], {}).items() if k in open_ids}
                    conflicts[str(batch["id"])] = batch["base"]
                    self.store.update(
                        "inputs",
                        {"id": row["id"]},
                        state=INPUT_WAITING,
                        batch_id=None,
                        last_probe=dumps(paths),
                        last_probe_tip=batch["tip"],
                        last_probe_base=batch["base"],
                        probe_conflicts=dumps(conflicts),
                    )
                self._release_claim(claim, "conflict", now)
                self.store.journal(
                    now, "probe-conflict", batch["id"], number=row["number"], paths=paths, against_main=against_main
                )
            return False
        record = result.commits[0]
        version = batch["version"] + 1
        with self.store.transaction():
            position = len(self._members(batch["id"]))
            self.store.insert(
                "members",
                batch_id=batch["id"],
                input_id=row["id"],
                source_pr=member.source_pr,
                number=row["number"],
                accepted_head=row["head_sha"],
                position=position,
                commit_sha=record.sha,
                provenance="clean",
                added_at=now,
            )
            self.store.update(
                "batches", {"id": batch["id"]}, version=version, tip=result.tip, urgent=batch["urgent"] or row["urgent"]
            )
            self.store.update(
                "inputs", {"id": row["id"]}, state=INPUT_INSERTED, batch_id=batch["id"], last_probe="inserted"
            )
            self._retain(self.batch_ref(batch["id"], version), result.tip)
            self._release_claim(claim, "inserted", now)
            if self.setting("publication_enabled") and self.setting("membership_label"):
                self._enqueue(
                    "labels",
                    f"member-label:{batch['id']}:{row['number']}",
                    {"number": row["number"], "add": [self.setting("membership_label")], "remove": []},
                )
            self.store.journal(
                now,
                "inserted",
                batch["id"],
                number=row["number"],
                version=version,
                commit=record.sha,
                empty=record.empty,
            )
        return True

    def _conflict_request(self, row: dict[str, Any], paths: list[str], now: float) -> None:
        if not self.setting("publication_enabled"):
            return
        body = (
            f"This PR conflicts with `{self.canonical_branch}` ("
            + ", ".join(f"`{path}`" for path in paths[:20])
            + "). Rebase it and approve it again."
        )
        self._enqueue(
            "comment",
            f"conflict:{row['number']}:{row['head_sha']}",
            {"number": row["number"], "body": body, "marker": f"<!-- {COMMENT_MARKER} conflict:{row['head_sha']} -->"},
        )

    # --- sealing -----------------------------------------------------------------------------------

    def _seal(self, now: float) -> None:
        if self._head() is not None:
            return
        batch = self._open_batch()
        if batch is None or not self._members(batch["id"]):
            return
        if self._state("inference_hold"):
            self._reasons.append("inference hold: not sealing")
            return
        blocked = self._external_head_blocks_sealing()
        if blocked:
            self._reasons.append(blocked)
            return
        if self._active_claims(batch["id"]):
            self._reasons.append(f"open batch {batch['id']} is claimed; sealing waits for the release")
            return
        with self.store.transaction():
            self.store.update("batches", {"id": batch["id"]}, sealed=1, state=BATCH_HEAD, sealed_at=now)
            self._count("heads_sealed")
            self.store.journal(
                now,
                "sealed",
                batch["id"],
                version=batch["version"],
                size=len(self._members(batch["id"])),
                urgent=batch["urgent"],
            )
            if self._canonical is not None and not self._open_batches():
                self._create_open_batch(self._canonical, now)

    # --- head reconciliation -----------------------------------------------------------------------

    def _reconcile_head(self, now: float) -> None:
        head = self._head()
        if head is None:
            return
        canonical = self._canonical
        assert canonical is not None
        task_claims = [c for c in self._active_claims(head["id"]) if c["task_id"] is not None]
        if task_claims:
            self._reasons.append(f"head {head['id']} is claimed by {', '.join(c['worker'] for c in task_claims)}")
            return
        if head["base"] != canonical:
            if not self._rebase_head(head, canonical, now):
                return
            head = self._head()
            assert head is not None
        task = self._open_task(head)
        if task is not None:
            self._reasons.append(
                f"head {head['id']} waits for an authorized worker to claim {task['kind']} task {task['id']}"
            )
            return
        self._ci(head, now)

    def _rebase_head(self, head: dict[str, Any], canonical: str, now: float) -> bool:
        """Replay the head onto the canonical commit; True when it is now based there."""
        task = self._open_task(head)
        if task is not None and task["kind"] == "rebase_conflict" and task["onto"] == canonical:
            self._reasons.append(f"head {head['id']} waits for a worker to resolve rebase conflict task {task['id']}")
            return False
        if self._active_claims(head["id"]):
            self._reasons.append(f"head {head['id']} is claimed")
            return False
        with self.store.transaction():
            claim = self._take_claim(head, "rebase", now)
        chain_id = f"rebase-{head['id']}-{secrets.token_hex(4)}"
        try:
            result = chain.replay(self.clone, head["base"], head["tip"], canonical, chain_id=chain_id)
        except chain.ChainError as error:
            with self.store.transaction():
                self._release_claim(claim, "error", now)
                self.store.journal(now, "rebase-error", head["id"], error=str(error))
            self._reasons.append(f"rebase of head {head['id']} failed: {error}")
            return False
        finally:
            chain.delete_chain_ref(self.clone, chain_id)
        if result.status == chain.CLEAN:
            version = head["version"] + 1
            by_pr = {record.source_pr: record for record in result.commits}
            with self.store.transaction():
                for member in self._members(head["id"]):
                    record = by_pr.get(member["source_pr"])
                    if record is not None:
                        self.store.update("members", {"id": member["id"]}, commit_sha=record.sha)
                self.store.update(
                    "batches", {"id": head["id"]}, version=version, base=canonical, tip=result.tip, expected_parent=None
                )
                for stale in self.store.rows("tasks", {"batch_id": head["id"], "state": TASK_OPEN}):
                    self.store.update(
                        "tasks", {"id": stale["id"]}, state=TASK_SUPERSEDED, resolved_at=now, resolution="rebased"
                    )
                self._retain(self.batch_ref(head["id"], version), result.tip)
                self._release_claim(claim, "rebased", now)
                self._count("head_rebases")
                self.store.journal(
                    now,
                    "rebased",
                    head["id"],
                    version=version,
                    base=canonical,
                    tip=result.tip,
                    landed_on_base=result.landed_on_base,
                )
            return True
        conflict = result.conflict
        diagnostics = [
            {
                "kind": "conflict",
                "source_pr": conflict.source_pr if conflict else None,
                "paths": list(conflict.conflict_paths) if conflict else [],
                "onto": canonical,
                "message": f"replaying the chain onto {canonical[:12]} conflicts at {conflict.source_pr if conflict else '?'}",
                "pending": [r.source_pr for r in result.commits if r.status == chain.PENDING],
                "clean_prefix_tip": result.tip,
                # Members the new base already carries (landed outside the queue): empty commits in the
                # clean prefix, or still pending; the agent keeps them empty rather than replaying them.
                "landed_on_base": result.landed_on_base,
            }
        ]
        with self.store.transaction():
            self._release_claim(claim, "conflict", now)
            self.store.journal(
                now,
                "rebase-conflict",
                head["id"],
                onto=canonical,
                source_pr=conflict.source_pr if conflict else None,
                landed_on_base=result.landed_on_base,
            )
        self._create_task(head, "rebase_conflict", canonical, diagnostics, now)
        self._reasons.append(f"head {head['id']} needs an agent to resolve the rebase conflict")
        return False

    # --- CI ----------------------------------------------------------------------------------------

    def _ci(self, head: dict[str, Any], now: float) -> None:
        attempt = self._current_attempt(head)
        if attempt is None or attempt["outcome"] == OUTCOME_SUPERSEDED:
            self._dispatch(head, now, retry=0)
            return
        if attempt["outcome"] == OUTCOME_PENDING:
            self._observe_attempt(head, attempt, now)
            attempt = self.store.row("ci_attempts", {"id": attempt["id"]}) or attempt
        if attempt["outcome"] == OUTCOME_PENDING:
            self._reasons.append(f"head {head['id']} waits for CI run {attempt['run_id'] or '(not yet found)'}")
        elif attempt["outcome"] == OUTCOME_INFRASTRUCTURE:
            retries = self.store.count(
                "ci_attempts",
                {"batch_id": head["id"], "batch_version": head["version"], "outcome": OUTCOME_INFRASTRUCTURE},
            )
            limit = int(self.setting("infrastructure_retry_limit"))
            if retries > limit:
                self._reasons.append(
                    f"head {head['id']}: {retries} infrastructure failures ({attempt['outcome_detail']}); retries exhausted, use retry_ci"
                )
                return
            backoff = int(self.setting("infrastructure_retry_backoff_seconds")) * (2 ** (retries - 1))
            due = (attempt["last_observed_at"] or attempt["dispatched_at"]) + backoff
            if now < due:
                self._reasons.append(f"head {head['id']}: re-dispatch after infrastructure failure at {due:.0f}")
                return
            self._dispatch(head, now, retry=retries)
        elif attempt["outcome"] == OUTCOME_SOURCE_FAILURE:
            if self._open_task(head) is None:
                self._create_task(
                    head, "repair", head["base"], loads(attempt["diagnostics"], []), now, ci_attempt_id=attempt["id"]
                )
            self._reasons.append(f"head {head['id']} waits for a worker to claim its repair task")
        elif attempt["outcome"] == OUTCOME_PASS:
            self._promote(head, attempt, now)

    def rebuild_head(self, repo: str, actor: str) -> dict[str, Any]:
        """Operator: rebuild the head's chain from its members' accepted heads and dispatch it again.

        For a head whose chain must be regenerated without a source change (a new commit message format,
        a chain tool fix): the open task is superseded, the members' commits are rebuilt on the head's
        base, the version advances so the next tick dispatches a fresh attempt. Refused while the head is
        claimed or when a member carries agent edits, which a rebuild from accepted heads would discard.
        """
        with self._lock:
            self._check_repo(repo)
            head = self._head()
            if head is None:
                return {"rebuilt": False, "reason": "no-head"}
            if self._active_claims(head["id"]):
                return {"rebuilt": False, "reason": "claimed"}
            members = self._members(head["id"])
            edited = [m["number"] for m in members if m["provenance"] != "clean"]
            if edited:
                return {"rebuilt": False, "reason": "agent-edited", "members": edited}
            now = self.clock()
            chain_id = f"rebuild-{head['id']}-{secrets.token_hex(4)}"
            try:
                for member in members:
                    if not self._fetch_head(member["number"], member["accepted_head"]):
                        raise chain.ChainError(f"accepted head {member['accepted_head']} is not available")
                result = chain.build(
                    self.clone, head["base"], [self._chain_member(m) for m in members], chain_id=chain_id
                )
            except chain.ChainError as error:
                self.store.journal(now, "rebuild-error", head["id"], actor=actor, error=str(error))
                return {"rebuilt": False, "reason": str(error)}
            finally:
                chain.delete_chain_ref(self.clone, chain_id)
            if result.status != chain.CLEAN:
                conflict = result.conflict
                self.store.journal(
                    now, "rebuild-conflict", head["id"], actor=actor, source_pr=conflict.source_pr if conflict else None
                )
                return {"rebuilt": False, "reason": "conflict", "source_pr": conflict.source_pr if conflict else None}
            version = head["version"] + 1
            by_pr = {record.source_pr: record for record in result.commits}
            with self.store.transaction():
                for member in members:
                    record = by_pr.get(member["source_pr"])
                    if record is not None:
                        self.store.update("members", {"id": member["id"]}, commit_sha=record.sha)
                self.store.update("batches", {"id": head["id"]}, version=version, tip=result.tip, expected_parent=None)
                superseded = 0
                for stale in self.store.rows("tasks", {"batch_id": head["id"], "state": TASK_OPEN}):
                    self.store.update(
                        "tasks", {"id": stale["id"]}, state=TASK_SUPERSEDED, resolved_at=now, resolution="rebuilt"
                    )
                    superseded += 1
                self._retain(self.batch_ref(head["id"], version), result.tip)
                self._count("head_rebuilds")
                self.store.journal(
                    now,
                    "rebuilt",
                    head["id"],
                    actor=actor,
                    version=version,
                    tip=result.tip,
                    tasks_superseded=superseded,
                    landed_on_base=result.landed_on_base,
                )
            self._wake.set()
            return {
                "rebuilt": True,
                "version": version,
                "tip": result.tip,
                "tasks_superseded": superseded,
                "landed_on_base": result.landed_on_base,
            }

    def retry_ci(self, repo: str, actor: str) -> dict[str, Any]:
        """Operator: forget the head's exhausted infrastructure failures so the next tick re-dispatches."""
        with self._lock:
            self._check_repo(repo)
            head = self._head()
            if head is None:
                return {"retried": False, "reason": "no-head"}
            now = self.clock()
            count = self.store.update(
                "ci_attempts",
                {"batch_id": head["id"], "batch_version": head["version"], "outcome": OUTCOME_INFRASTRUCTURE},
                outcome=OUTCOME_SUPERSEDED,
            )
            self.store.journal(now, "retry-ci", head["id"], actor=actor, forgotten=count)
            self._wake.set()
            return {"retried": True, "forgotten": count}

    def _dispatch(self, head: dict[str, Any], now: float, retry: int) -> None:
        if not self.setting("publication_enabled"):
            self._reasons.append(
                f"publication disabled: would dispatch CI for head {head['id']} v{head['version']} ({head['tip'][:12]} on {head['base'][:12]})"
            )
            return
        members = [m["number"] for m in self._members(head["id"])]
        # The attempt row is reserved first because its id names the staging candidate branch that the
        # feed reports and CI verifies; a failed push or dispatch removes the row again.
        attempt_id = self.store.insert(
            "ci_attempts",
            batch_id=head["id"],
            batch_version=head["version"],
            head_sha=head["tip"],
            base_sha=head["base"],
            base_ref=self.canonical_branch,
            dispatched_at=now,
            infrastructure_retry=retry,
            last_progress_at=now,
        )
        branch = self.staging_candidate_ref(attempt_id, head["version"])
        try:
            # The candidate exists only in the service's clone until it is retained on GitHub; CI checks it
            # out by SHA, so the retained ref is pushed before the dispatch rather than by the outbox later.
            # The staging candidate branch is the same commit under the name pr-build.yml's train identity
            # check fetches and compares with the dispatched head.
            retained_ref = self.batch_ref(head["id"], head["version"])
            retained = self.store.row("outbox", {"key": f"retain:{retained_ref}:{head['tip']}"})
            if retained is None or retained["state"] != "done":
                self.github.update_ref(self.repository, retained_ref, head["tip"])
                if retained is not None:
                    self._outbox_done(retained, loads(retained["history"], []), "retained-by-dispatch", now)
            self.github.update_ref(self.repository, f"refs/heads/{branch}", head["tip"])
            dispatch_id = self.github.dispatch_candidate(
                self.repository,
                head_sha=head["tip"],
                base_sha=head["base"],
                base_ref=self.canonical_branch,
                batch_id=str(head["id"]),
                members=members,
            )
        except Exception as error:  # noqa: BLE001
            with self.store.transaction():
                self.store.execute("DELETE FROM ci_attempts WHERE id = ?", (attempt_id,))
            self._reasons.append(f"dispatch of head {head['id']} failed: {error}")
            self.store.journal(now, "dispatch-failed", head["id"], error=str(error))
            return
        with self.store.transaction():
            self.store.update("ci_attempts", {"id": attempt_id}, dispatch_id=dispatch_id)
            if head["first_dispatch_at"] is None:
                self.store.update("batches", {"id": head["id"]}, first_dispatch_at=now)
            self._count("ci_dispatches")
            if retry:
                self._count("ci_redispatches")
            self.store.journal(
                now,
                "dispatched",
                head["id"],
                version=head["version"],
                tip=head["tip"],
                dispatch_id=dispatch_id,
                retry=retry,
            )

    def _observe_attempt(self, head: dict[str, Any], attempt: dict[str, Any], now: float) -> None:
        interval = int(self.setting("polling_interval_seconds"))
        due = (
            attempt["needs_observation"]
            or attempt["last_observed_at"] is None
            or now - attempt["last_observed_at"] >= interval
        )
        if not due:
            return
        try:
            run = self.github.find_candidate_run(
                self.repository,
                batch_id=str(head["id"]),
                head_sha=attempt["head_sha"],
                base_sha=attempt["base_sha"],
                since=attempt["dispatched_at"],
            )
        except Exception as error:  # noqa: BLE001
            self._reasons.append(f"CI observation failed: {error}")
            return
        values: dict[str, Any] = {"last_observed_at": now, "needs_observation": 0}
        if run is None:
            if now - attempt["dispatched_at"] >= int(self.setting("dispatch_timeout_seconds")):
                values.update(outcome=OUTCOME_INFRASTRUCTURE, outcome_detail="no-run: the dispatch produced no run")
                self.store.journal(
                    now, "ci-infrastructure", head["id"], attempt_id=attempt["id"], detail=values["outcome_detail"]
                )
            self.store.update("ci_attempts", {"id": attempt["id"]}, **values)
            return
        progress = [t for t in (run.created_at, run.started_at, run.completed_at) if t]
        for phase in run.phases:
            progress.extend(t for t in (phase.started_at, phase.completed_at) if t)
        last_progress = max(progress) if progress else attempt["last_progress_at"]
        phases = [vars(phase) for phase in run.phases]
        if phases != loads(attempt["phases"], []) and not progress:
            last_progress = now
        values.update(
            run_id=run.run_id,
            run_attempt=run.attempt,
            run_status=run.status,
            run_conclusion=run.conclusion,
            run_created_at=run.created_at,
            run_started_at=run.started_at,
            run_completed_at=run.completed_at,
            runner_name=run.runner_name,
            runner_labels=dumps(list(run.runner_labels)),
            host=run.host,
            phases=dumps(phases),
            html_url=run.html_url,
            last_progress_at=max(last_progress or 0, attempt["last_progress_at"] or 0) or now,
            diagnostics=dumps(list(run.diagnostics)),
        )
        if run.status != "completed":
            if now - values["last_progress_at"] >= int(self.setting("lost_run_seconds")):
                values.update(
                    outcome=OUTCOME_INFRASTRUCTURE,
                    outcome_detail=f"lost-run: no progress for {now - values['last_progress_at']:.0f}s",
                )
                self.store.journal(
                    now, "ci-infrastructure", head["id"], attempt_id=attempt["id"], detail=values["outcome_detail"]
                )
            self.store.update("ci_attempts", {"id": attempt["id"]}, **values)
            return
        if run.conclusion not in ("success", "failure"):
            values.update(outcome=OUTCOME_INFRASTRUCTURE, outcome_detail=f"run-{run.conclusion or 'unknown'}")
            self.store.journal(
                now, "ci-infrastructure", head["id"], attempt_id=attempt["id"], detail=values["outcome_detail"]
            )
            self.store.update("ci_attempts", {"id": attempt["id"]}, **values)
            return
        # Every verdict context is read and bound the same way: a status counts only when its
        # `target_url` names the candidate's own run and it is no longer pending.
        statuses = {
            context: self.github.commit_status(self.repository, attempt["head_sha"], context)
            for context in VERDICT_CONTEXTS
        }
        bound = {
            context: status is not None and status.run_id == run.run_id and status.state != "pending"
            for context, status in statuses.items()
        }
        values["verdicts"] = dumps(
            {
                context: {
                    # `absent`: no status; `unbound`: a status from some other run; else the bound state.
                    "state": "absent"
                    if status is None
                    else ("unbound" if status.run_id != run.run_id else status.state),
                    "bound": bound[context],
                    "description": status.description if bound[context] else "",
                    "target_url": status.target_url if bound[context] else "",
                }
                for context, status in statuses.items()
            }
        )
        status = statuses[BUILD_CONTEXT]
        if status is None or not bound[BUILD_CONTEXT]:
            # No status from the candidate's own completed run: never a verdict (scenario 19).
            values.update(
                outcome=OUTCOME_INFRASTRUCTURE,
                outcome_detail="missing-status: the completed run posted no bound build status",
            )
            self.store.journal(
                now,
                "ci-infrastructure",
                head["id"],
                attempt_id=attempt["id"],
                detail=values["outcome_detail"],
                status_run=status.run_id if status else None,
            )
            self.store.update("ci_attempts", {"id": attempt["id"]}, **values)
            return
        if status.state == "success":
            unbound = [context for context in VERDICT_CONTEXTS if not bound[context]]
            if unbound:
                # The finalizer posts all three statuses in the one step that posts `build`; a bound
                # `build` with the others absent is that step still in flight (or a post that failed).
                # Look again for a little while, then treat it as the missing `build` case above.
                if now - (run.completed_at or now) < LOG_GRACE_SECONDS:
                    values.update(needs_observation=1)
                    self._reasons.append(
                        f"head {head['id']}: run {run.run_id} posted build, waiting for {', '.join(unbound)}"
                    )
                    self.store.update("ci_attempts", {"id": attempt["id"]}, **values)
                    return
                values.update(
                    outcome=OUTCOME_INFRASTRUCTURE,
                    outcome_detail=f"missing-status: the completed run posted no bound {', '.join(unbound)} status",
                )
                self.store.journal(
                    now, "ci-infrastructure", head["id"], attempt_id=attempt["id"], detail=values["outcome_detail"]
                )
                self.store.update("ci_attempts", {"id": attempt["id"]}, **values)
                return
            failed = [context for context in VERDICT_CONTEXTS if statuses[context].state != "success"]
            if failed:
                # The build is green but a policy verdict is red: a property of the head's source (its
                # paths, diff size, pin change), carried as the status said it, attributed to nobody.
                findings = [
                    {
                        "kind": "status",
                        "context": context,
                        "module": "",
                        "message": statuses[context].description,
                        "target_url": statuses[context].target_url,
                    }
                    for context in failed
                ]
                values.update(
                    outcome=OUTCOME_SOURCE_FAILURE,
                    outcome_detail="; ".join(f"{context}: {statuses[context].description}" for context in failed),
                    diagnostics=dumps(findings),
                )
                self._count("ci_source_failures")
                self.store.journal(
                    now,
                    "ci-source-failure",
                    head["id"],
                    attempt_id=attempt["id"],
                    run_id=run.run_id,
                    findings=len(findings),
                    contexts=failed,
                )
            else:
                values.update(outcome=OUTCOME_PASS, outcome_detail=status.description)
                self._count("ci_passes")
                self.store.journal(now, "ci-pass", head["id"], attempt_id=attempt["id"], run_id=run.run_id)
        else:
            source = [d for d in run.diagnostics if d.get("kind") != "infrastructure"]
            infrastructure = [d for d in run.diagnostics if d.get("kind") == "infrastructure"]
            log_pending = (
                not source
                and infrastructure
                and all(d.get("reason") == "log_unavailable" for d in infrastructure)
                and not status.description.lower().startswith("infrastructure")
                and now - (run.completed_at or now) < LOG_GRACE_SECONDS
            )
            if log_pending:
                # GitHub publishes a job's log a little after the run completes; a failed build whose log
                # cannot be read yet is neither a source nor an infrastructure verdict. Look again.
                values.update(needs_observation=1)
                self._reasons.append(f"head {head['id']}: run {run.run_id} failed, waiting for its log")
                self.store.update("ci_attempts", {"id": attempt["id"]}, **values)
                return
            if status.description.lower().startswith("infrastructure") or (infrastructure and not source):
                values.update(
                    outcome=OUTCOME_INFRASTRUCTURE, outcome_detail=f"infrastructure-failure: {status.description}"
                )
                self.store.journal(
                    now, "ci-infrastructure", head["id"], attempt_id=attempt["id"], detail=values["outcome_detail"]
                )
            else:
                findings = source or [
                    {"kind": "status", "module": "", "message": status.description, "target_url": status.target_url}
                ]
                values.update(
                    outcome=OUTCOME_SOURCE_FAILURE, outcome_detail=status.description, diagnostics=dumps(findings)
                )
                self._count("ci_source_failures")
                self.store.journal(
                    now,
                    "ci-source-failure",
                    head["id"],
                    attempt_id=attempt["id"],
                    run_id=run.run_id,
                    findings=len(findings),
                )
        self.store.update("ci_attempts", {"id": attempt["id"]}, **values)

    # --- promotion and landing ---------------------------------------------------------------------

    def _promote(self, head: dict[str, Any], attempt: dict[str, Any], now: float) -> None:
        if not self.setting("publication_enabled"):
            self._reasons.append(
                f"publication disabled: would fast-forward {self.canonical_branch} from {head['base'][:12]} to {head['tip'][:12]}"
            )
            return
        key = f"promote:{head['id']}:{head['version']}:{head['tip']}"
        if self.store.row("outbox", {"key": key}) is None:
            with self.store.transaction():
                self.store.update("batches", {"id": head["id"]}, expected_parent=head["base"])
                self._enqueue(
                    "promote",
                    key,
                    {
                        "batch_id": head["id"],
                        "version": head["version"],
                        "expected_parent": head["base"],
                        "tip": head["tip"],
                        "attempt_id": attempt["id"],
                    },
                )
                self.store.journal(now, "promotion-queued", head["id"], version=head["version"], tip=head["tip"])
        else:
            self._reasons.append(f"head {head['id']} promotion pending in the outbox")

    def _landed(self, batch: dict[str, Any], how: str, now: float) -> None:
        batch = self.store.row("batches", {"id": batch["id"]}) or batch
        if batch["state"] == BATCH_LANDED:
            return
        members = self._members(batch["id"])
        with self.store.transaction():
            self.store.update("batches", {"id": batch["id"]}, state=BATCH_LANDED, landed_at=now, landed_how=how)
            for member in members:
                self.store.update(
                    "inputs",
                    {"id": member["input_id"]},
                    state=INPUT_LANDED,
                    landed_at=now,
                    landed_sha=member["commit_sha"],
                )
            for stale in self.store.rows("tasks", {"batch_id": batch["id"], "state": TASK_OPEN}):
                self.store.update(
                    "tasks", {"id": stale["id"]}, state=TASK_SUPERSEDED, resolved_at=now, resolution="landed"
                )
            for claim in self._active_claims(batch["id"]):
                self._release_claim(claim, "landed", now)
            self._count("heads_landed")
            self._count("prs_landed", len(members))
            self._count("prs_edited_landed", sum(1 for m in members if m["provenance"] == "edited"))
            if batch["first_dispatch_at"]:
                cycles = self._state("head_cycle_seconds", [])
                cycles.append(round(now - batch["first_dispatch_at"], 1))
                self._set_state("head_cycle_seconds", cycles[-50:])
            self.store.journal(
                now, "landed", batch["id"], how=how, tip=batch["tip"], members=[m["number"] for m in members]
            )
            if self.setting("publication_enabled"):
                for member in members:
                    self._enqueue_landing_effects(batch, member)
        self._wake.set()

    def _enqueue_landing_effects(self, batch: dict[str, Any], member: dict[str, Any]) -> None:
        number = member["number"]
        body = self.member_comment(batch, member)
        marker = f"<!-- {COMMENT_MARKER} landed:{self.repository}#{number}:{batch['id']}:{member['commit_sha']} -->"
        self._enqueue(
            "comment",
            f"landed-comment:{batch['id']}:{number}",
            {"number": number, "body": body + "\n\n" + marker, "marker": marker},
        )
        self._enqueue("close_pr", f"landed-close:{batch['id']}:{number}", {"number": number})

    def member_comment(self, batch: dict[str, Any], member: dict[str, Any]) -> str:
        """The one consolidated landing comment for a member: Bors's notice, plus the agent's explanation when edited."""
        attempt = self.store.row(
            "ci_attempts",
            {"batch_id": batch["id"], "batch_version": batch["version"], "outcome": OUTCOME_PASS},
            order="id DESC",
        )
        link = f"[{BUILD_CONTEXT}]({attempt['html_url']})" if attempt and attempt["html_url"] else BUILD_CONTEXT
        lines = [
            f"Pull request successfully merged into {self.canonical_branch}.",
            "",
            "Build succeeded:",
            f"  * {link}",
            "",
            f"Merged revision: `{member['accepted_head']}`.",
        ]
        if member["provenance"] == "edited":
            commit_url = f"https://github.com/{self.repository}/commit/{member['commit_sha']}"
            lines += [
                "",
                f"The merge queue edited this contribution while integrating batch {batch['id']}; it landed as [`{member['commit_sha'][:12]}`]({commit_url}).",
                "",
            ]
            lines += [f"> {line}" if line else ">" for line in member["explanation"].splitlines()]
            edited = [sha for sha in loads(member["edited_shas"], []) if sha and sha != member["commit_sha"]]
            if edited:
                lines += [
                    "",
                    "Edited revisions: "
                    + ", ".join(f"[`{sha[:12]}`](https://github.com/{self.repository}/commit/{sha})" for sha in edited)
                    + ".",
                ]
            mentioned = self._mentioned_prs(member["explanation"], exclude=member["number"])
            if mentioned:
                lines += [
                    "",
                    "Related PRs: "
                    + ", ".join(f"[#{n}](https://github.com/{self.repository}/pull/{n})" for n in mentioned)
                    + ".",
                ]
            lines += ["", f"The source is retained under `{self.batch_ref(batch['id'], batch['version'])}`."]
        return "\n".join(lines)

    def _mentioned_prs(self, text: str, exclude: int) -> list[int]:
        found: list[int] = []
        for match in _PR_MENTION_RE.finditer(text):
            repo = match["repo"]
            number = int(match["number"])
            if (repo is None or repo == self.repository) and number != exclude and number not in found:
                found.append(number)
        return found

    # --- outbox ------------------------------------------------------------------------------------

    def _enqueue(self, kind: str, key: str, payload: dict[str, Any]) -> None:
        now = self.clock()
        if self.store.row("outbox", {"key": key}) is not None:
            return
        self.store.insert("outbox", kind=kind, key=key, payload=dumps(payload), next_attempt_at=now, created_at=now)

    def _drain_outbox(self, now: float) -> None:
        """Perform the due entries: several retained refs go in one push, the rest one by one, until
        `outbox_drain_budget_seconds` of wall time is spent; what remains waits for the next tick,
        which the loop is woken to run at once."""
        started = time.monotonic()
        budget = float(self.setting("outbox_drain_budget_seconds"))
        due = [e for e in self.store.rows("outbox", {"state": "pending"}, order="id") if e["next_attempt_at"] <= now]
        performed = 0
        retains = [e for e in due if e["kind"] == "retain_ref"]
        if len(retains) > 1 and hasattr(self.github, "update_refs"):
            performed += 1
            if self._retain_batch(retains, now):
                due = [e for e in due if e["kind"] != "retain_ref"]
        for index, entry in enumerate(due):
            if performed and time.monotonic() - started >= budget:
                self._reasons.append(f"outbox drain paused with {len(due) - index} entries pending (budget)")
                self._wake.set()
                return
            performed += 1
            history = loads(entry["history"], [])
            try:
                result = self._perform(entry["kind"], loads(entry["payload"], {}), now)
            except Exception as error:  # noqa: BLE001 - every adapter failure is recorded and retried
                self._outbox_failed(entry, history, error, now)
                continue
            self._outbox_done(entry, history, result, now)

    def _retain_batch(self, entries: list[dict[str, Any]], now: float) -> bool:
        """Push the entries' retained refs in one `git push` (one connection, one object upload) instead of
        one push each. On failure nothing is recorded: the caller pushes them singly, so one bad ref
        does not block the rest."""
        payloads = [loads(entry["payload"], {}) for entry in entries]
        updates = [(payload["ref"], payload["sha"]) for payload in payloads]
        try:
            self.github.update_refs(self.repository, updates)
        except Exception as error:  # noqa: BLE001 - the single pushes record their own failures
            self.log.warning("batched push of %s retained refs failed, pushing singly: %s", len(updates), error)
            return False
        for entry in entries:
            self._outbox_done(entry, loads(entry["history"], []), "retained", now)
        return True

    def _outbox_done(self, entry: dict[str, Any], history: list[Any], result: str, now: float) -> None:
        history.append({"at": now, "result": result})
        self.store.update(
            "outbox",
            {"id": entry["id"]},
            state="done",
            attempts=entry["attempts"] + 1,
            history=dumps(history[-20:]),
            done_at=now,
            result=result,
        )

    def _outbox_failed(self, entry: dict[str, Any], history: list[Any], error: Exception, now: float) -> None:
        attempts = entry["attempts"] + 1
        backoff = min(
            int(self.setting("outbox_retry_seconds")) * (2 ** (attempts - 1)),
            int(self.setting("outbox_retry_max_seconds")),
        )
        history.append({"at": now, "error": str(error)[:500]})
        self.store.update(
            "outbox",
            {"id": entry["id"]},
            attempts=attempts,
            history=dumps(history[-20:]),
            next_attempt_at=now + backoff,
        )
        self.log.warning("outbox %s %s failed (attempt %s): %s", entry["kind"], entry["key"], attempts, error)

    def _perform(self, kind: str, payload: dict[str, Any], now: float) -> str:
        repo = self.repository
        if kind == "retain_ref":
            self.github.update_ref(repo, payload["ref"], payload["sha"])
            return "retained"
        if kind == "comment":
            return "comment " + str(
                self.github.post_comment(repo, payload["number"], payload["body"], payload["marker"])
            )
        if kind == "labels":
            self.github.set_labels(repo, payload["number"], payload.get("add", []), payload.get("remove", []))
            return "labels"
        if kind == "close_pr":
            pull = self.github.fetch_pr(repo, payload["number"])
            if pull.state == "closed":
                return "already-closed"
            title = pull.title if pull.title.startswith(MERGED_TITLE_PREFIX) else MERGED_TITLE_PREFIX + pull.title
            self.github.close_pr(repo, payload["number"], title)
            return "closed"
        if kind == "promote":
            return self._perform_promotion(payload, now)
        raise ControllerError("unknown-outbox-kind", kind)

    def _perform_promotion(self, payload: dict[str, Any], now: float) -> str:
        batch = self.store.row("batches", {"id": payload["batch_id"]})
        if batch is None:
            return "no-batch"
        if batch["state"] == BATCH_LANDED:
            return "already-landed"
        if batch["version"] != payload["version"] or batch["tip"] != payload["tip"]:
            return "stale: batch moved on"
        # A lost response from an earlier attempt: the branch may already carry the tip.
        canonical = self._fetch_canonical()
        if canonical == payload["tip"] or chain.is_ancestor(self.clone, payload["tip"], canonical):
            self._canonical = canonical
            self._set_state("canonical_sha", canonical)
            self._landed(batch, "promoted", now)
            return "already-visible"
        result = self.github.fast_forward(
            self.repository, self.canonical_branch, payload["expected_parent"], payload["tip"]
        )
        if result.ok:
            self._canonical = payload["tip"]
            self._set_state("canonical_sha", payload["tip"])
            self._retain_local(self._canonical_ref(), payload["tip"])
            self._landed(batch, "promoted", now)
            return "fast-forwarded"
        if result.actual_parent and result.actual_parent != payload["expected_parent"]:
            with self.store.transaction():
                self.store.update("batches", {"id": batch["id"]}, expected_parent=None)
                self._count("promotion_refused_parent_changed")
                self.store.journal(
                    now,
                    "promotion-refused",
                    batch["id"],
                    expected=payload["expected_parent"],
                    actual=result.actual_parent,
                )
            self._wake.set()
            return f"parent-changed: {result.actual_parent}"
        self._count("promotion_failures")
        raise ControllerError("promotion-failed", result.error or "fast-forward refused")

    # --- status ------------------------------------------------------------------------------------

    def status(self, *, wait: float = STATUS_LOCK_WAIT_SECONDS) -> dict[str, Any]:
        """The status document, read live when the controller lock is free within `wait` seconds.

        Otherwise (a tick is holding the lock) the snapshot the last tick or operator action took is
        served, with `snapshot: true` and its age in `stale_seconds`, so that the public endpoints
        answer during a long tick instead of hanging on it.
        """
        if not self._lock.acquire(timeout=wait):
            return self._stale_status()
        try:
            return self._status()
        finally:
            self._lock.release()

    def _refresh_snapshot(self) -> None:
        """Take the snapshot `status()` serves while the lock is held; called under the lock after a mutation."""
        try:
            self._status_snapshot = (time.monotonic(), self._status())
        except Exception as error:  # noqa: BLE001 - a snapshot failure must not fail the tick
            self.log.warning("status snapshot failed: %s", error)

    def _stale_status(self) -> dict[str, Any]:
        if self._status_snapshot is None:
            return {
                "schema": STATUS_SCHEMA,
                "repository": self.repository,
                "canonical_branch": self.canonical_branch,
                "service": {
                    "revision": self.revision,
                    "started_at": self.started_at,
                    "now": self.clock(),
                    "last_tick_at": None,
                },
                "reasons": ["the first reconciliation pass has not completed"],
                "snapshot": True,
                "stale_seconds": None,
            }
        taken, document = self._status_snapshot
        return {**document, "snapshot": True, "stale_seconds": round(time.monotonic() - taken, 3)}

    def _status(self) -> dict[str, Any]:
        with self._lock:
            now = self.clock()
            settings = self.effective_settings()
            waiting = self._waiting()
            open_batches = self._open_batches()
            open_batch = open_batches[0] if open_batches else None
            head = self._head()
            hold = self._state("inference_hold")
            alerts: list[str] = []
            head_status = None
            if head is not None:
                head_status = self._head_status(head, now, settings, alerts)
            if hold and now - hold["since"] > 3600:
                alerts.append(f"inference hold older than an hour (since {hold['since']:.0f}): {hold['last_error']}")
            counters = self._state("counters", {})
            pending = self.store.rows("outbox", {"state": "pending"}, order="id")
            if any(e["attempts"] and e["kind"] == "promote" for e in pending):
                alerts.append("promotion is failing; see outbox history")
            tasks = self.store.rows("tasks", {"state": TASK_OPEN}, order="id")
            return {
                "schema": STATUS_SCHEMA,
                "repository": self.repository,
                "canonical_branch": self.canonical_branch,
                "canonical_sha": self._canonical,
                "service": {
                    "revision": self.revision,
                    "started_at": self.started_at,
                    "now": now,
                    "last_tick_at": self._state("last_tick_at"),
                },
                "settings": settings,
                "publication_enabled": bool(settings["publication_enabled"]),
                "inference_hold": hold,
                "waiting": {
                    "count": len(waiting),
                    "urgent": sum(1 for row in waiting if row["urgent"]),
                    "oldest_approved_at": min(row["approved_at"] for row in waiting) if waiting else None,
                    "oldest_age_seconds": (now - min(row["approved_at"] for row in waiting)) if waiting else None,
                    "max_passes_waited": max((row["passes_waited"] for row in waiting), default=0),
                    "inputs": [self._input_status(row) for row in waiting],
                },
                "open_batch": None if open_batch is None else self._batch_status(open_batch, now),
                "open_batches": [self._open_batch_summary(batch, now) for batch in open_batches],
                "head": head_status,
                "external_head": self._external_head_status(),
                "approvals": {
                    "webhook": counters.get("approvals_webhook", 0),
                    "polled": counters.get("approvals_polled", 0),
                    "revoked_by_poll": counters.get("approvals_revoked_by_poll", 0),
                    "last_poll": self._state("approval_poll"),
                },
                "tasks": [self._task_status(task) for task in tasks],
                "outbox": {"pending": len(pending), "entries": [self._outbox_status(e) for e in pending]},
                "reasons": list(self._reasons),
                "alerts": alerts,
                "counters": counters,
                "head_cycle_seconds": self._state("head_cycle_seconds", []),
                "insertion_passes": self._state("insertion_passes", 0),
                "max_passes_waited": self._state("max_passes_waited", 0),
            }

    def _external_head_status(self) -> dict[str, Any] | None:
        record = self._state("external_head")
        if record is None:
            return None
        members = [int(m["pr"]) for m in record.get("members", [])]
        landed = list(record.get("landed_members", []))
        return {
            "batch_id": record.get("batch_id"),
            "head_sha": record.get("head_sha"),
            "base_sha": record.get("base_sha"),
            "state": record["state"],
            "members": len(members),
            "landed_members": len(landed),
            "pending_members": sorted(set(members) - set(landed)),
            "recorded_at": record.get("recorded_at"),
            "feed_observed_at": record.get("feed_observed_at"),
            "landed_at": record.get("landed_at"),
            "released_at": record.get("released_at"),
            "release_reason": record.get("release_reason"),
        }

    def _input_status(self, row: dict[str, Any]) -> dict[str, Any]:
        return {
            "number": row["number"],
            "head_sha": row["head_sha"],
            "approved_at": row["approved_at"],
            "priority": row["priority"],
            "urgent": bool(row["urgent"]),
            "passes_waited": row["passes_waited"],
            "probes": row["probes"],
            "last_probe": row["last_probe"],
        }

    def _open_batch_summary(self, batch: dict[str, Any], now: float) -> dict[str, Any]:
        members = self._members(batch["id"])
        return {
            "id": batch["id"],
            "version": batch["version"],
            "base": batch["base"],
            "tip": batch["tip"],
            "size": len(members),
            "urgent": bool(batch["urgent"]),
            "oldest_member_age_seconds": (now - min(m["added_at"] for m in members)) if members else None,
        }

    def _batch_status(self, batch: dict[str, Any], now: float) -> dict[str, Any]:
        members = self._members(batch["id"])
        claims = self._active_claims(batch["id"])
        return {
            "id": batch["id"],
            "version": batch["version"],
            "base": batch["base"],
            "tip": batch["tip"],
            "size": len(members),
            "urgent": bool(batch["urgent"]),
            "oldest_member_age_seconds": (now - min(m["added_at"] for m in members)) if members else None,
            "members": [
                {
                    "number": m["number"],
                    "source_pr": m["source_pr"],
                    "accepted_head": m["accepted_head"],
                    "commit": m["commit_sha"],
                    "provenance": m["provenance"],
                    "task_id": m["task_id"],
                }
                for m in members
            ],
            "claim": None
            if not claims
            else {
                "operation": claims[0]["operation"],
                "worker": claims[0]["worker"],
                "expires_at": claims[0]["expires_at"],
                "task_id": claims[0]["task_id"],
            },
            "claims": [
                {
                    "operation": c["operation"],
                    "worker": c["worker"],
                    "expires_at": c["expires_at"],
                    "task_id": c["task_id"],
                }
                for c in claims
            ],
            "retained_ref": self.batch_ref(batch["id"], batch["version"]),
        }

    def _head_status(
        self, head: dict[str, Any], now: float, settings: dict[str, Any], alerts: list[str]
    ) -> dict[str, Any]:
        info = self._batch_status(head, now)
        info["sealed_at"] = head["sealed_at"]
        info["expected_parent"] = head["expected_parent"]
        info["first_dispatch_at"] = head["first_dispatch_at"]
        info["based_on_canonical"] = head["base"] == self._canonical
        task = self._open_task(head)
        info["task"] = None if task is None else self._task_status(task)
        attempt = self._current_attempt(head)
        info["ci"] = None
        info["stall"] = None
        if head["base"] != self._canonical:
            stage = "rebase_conflict" if task and task["kind"] == "rebase_conflict" else "rebase"
        elif task is not None:
            stage = "repair"
        elif attempt is None:
            stage = "dispatch"
        elif attempt["outcome"] == OUTCOME_PASS:
            stage = "promote"
        elif attempt["outcome"] == OUTCOME_PENDING:
            stage = "ci"
        else:
            stage = attempt["outcome"]
        info["stage"] = stage
        if attempt is not None:
            ci = {
                "attempt_id": attempt["id"],
                "dispatch_id": attempt["dispatch_id"],
                "dispatched_at": attempt["dispatched_at"],
                "run_id": attempt["run_id"],
                "run_attempt": attempt["run_attempt"],
                "status": attempt["run_status"],
                "conclusion": attempt["run_conclusion"],
                "created_at": attempt["run_created_at"],
                "started_at": attempt["run_started_at"],
                "completed_at": attempt["run_completed_at"],
                "runner": attempt["runner_name"],
                "runner_labels": loads(attempt["runner_labels"], []),
                "host": attempt["host"],
                "phases": loads(attempt["phases"], []),
                "last_progress_at": attempt["last_progress_at"],
                "last_observed_at": attempt["last_observed_at"],
                "outcome": attempt["outcome"],
                "outcome_detail": attempt["outcome_detail"],
                "verdicts": loads(attempt["verdicts"], {}),
                "html_url": attempt["html_url"],
                "infrastructure_retry": attempt["infrastructure_retry"],
            }
            info["ci"] = ci
            if attempt["outcome"] == OUTCOME_PENDING and attempt["last_progress_at"]:
                silence = now - attempt["last_progress_at"]
                interval = int(settings["stall_interval_seconds"])
                if silence >= interval:
                    since = attempt["last_progress_at"] + interval
                    info["stall"] = {
                        "since": since,
                        "silence_seconds": silence,
                        "phase": (ci["phases"][-1]["name"] if ci["phases"] else None),
                    }
                    if silence >= 2 * interval:
                        alerts.append(
                            f"head {head['id']} build stalled for {silence:.0f}s on {attempt['runner_name'] or 'unknown runner'} ({attempt['host'] or 'unknown host'})"
                        )
        failures = self.store.count("journal", {"kind": "chain-invalid", "batch_id": head["id"]})
        if failures >= 2:
            alerts.append(f"head {head['id']}: a submitted chain failed validation {failures} times")
        return info

    def _task_status(self, task: dict[str, Any]) -> dict[str, Any]:
        claims = self._active_claims(task["batch_id"], task_id=task["id"])
        return {
            "task_id": task["id"],
            "kind": task["kind"],
            "batch_id": task["batch_id"],
            "batch_version": task["batch_version"],
            "created_at": task["created_at"],
            "claims": task["claims"],
            "submissions": task["submissions"],
            "active_claims": [{"worker": c["worker"], "expires_at": c["expires_at"]} for c in claims],
            "diagnostics": loads(task["diagnostics"], []),
        }

    @staticmethod
    def _outbox_status(entry: dict[str, Any]) -> dict[str, Any]:
        return {
            "kind": entry["kind"],
            "key": entry["key"],
            "attempts": entry["attempts"],
            "next_attempt_at": entry["next_attempt_at"],
            "history": loads(entry["history"], []),
        }

    # --- test and tooling helpers ------------------------------------------------------------------

    def batches(self, state: str | None = None) -> list[dict[str, Any]]:
        where: dict[str, Any] = {"repository": self.repository}
        if state:
            where["state"] = state
        return self.store.rows("batches", where, order="id")

    def members(self, batch_id: int) -> list[dict[str, Any]]:
        return self._members(batch_id)

    def inputs(self, state: str | Sequence[str] | None = None) -> list[dict[str, Any]]:
        where: dict[str, Any] = {"repository": self.repository}
        if state:
            where["state"] = state
        return self.store.rows("inputs", where, order="id")

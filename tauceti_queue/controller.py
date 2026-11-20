"""The AI merge queue controller: records, claims, insertion, sealing, head reconciliation, tasks, status.

The queue has unsealed batches receiving scripted insertions and a target of two sealed active
batches, including at most one head. The head is rebased, built, repaired in place by agents, and
fast-forwarded onto the canonical branch. The controller is one object shared by the
reconciliation loop (`tauceti_queue.loop`) and the HTTP server; every public method takes the
controller lock (`ControllerLock`: the worker endpoints take it ahead of the tick, which lets
go of it around its GitHub I/O), and every record mutation is a `Store` transaction.

The controller receives separate source and execution ports; the existing combined GitHub adapter
supplies both by default. Git work (squash, replay, validation, provenance) is `tauceti_queue.chain`
run against a local clone, into which the source port fetches member heads and the canonical branch. Source side effects
(promotion, comments, closes, labels, ref retention) go through the outbox table with idempotency
keys and are drained with retries; restart reconciliation (`recover`) releases in-process claims and
leaves the outbox and worker claims to be re-driven.

Documentation of the records, state machine, task API and status schema: `docs/queue/controller.md`.
"""

from __future__ import annotations

import concurrent.futures
import contextlib
import dataclasses
import hashlib
import logging
import random
import re
import secrets
import subprocess
import threading
import time
from collections.abc import Callable, Iterator, Sequence
from datetime import datetime
from typing import Any

from . import admission_planning, chain, declaration_names, escalation, stuck_heads
from .eligibility import KEEP as BLOCKING_LABELS
from .mixed_identity import SourceNumber
from .ports import (
    AGENT_INSTRUCTION,
    REPAIR_CHECKPOINT,
    Adoption,
    CandidateRun,
    DispatchNotAccepted,
    ExecutionPort,
    GitHubUnavailable,
    PromotionRefused,
    SourcePort,
    Task,
    TaskMember,
)
from .source_authority import NativeQueueAuthority, SourceAuthorityUnavailable, principal_key
from .source_ci import NoOwner, RunDeleted
from .stale_cache import StaleCache
from .store import Store, dumps, loads
from .task_identity import NATIVE_GIT_REFS, TaskBinding, native_retained_ref, native_submission_ref

STATUS_SCHEMA = "tauceti-queue.status/v1"
BUILD_CONTEXT = "build"
# The commit statuses pr-build.yml's finalizer posts on the candidate head, every one of which Bors
# required (`bors.toml` `status`): `build` is the build's result, `bump-guard` the Lake-pin validation
# and `scope` the merge-policy verdict (restricted paths, diff size, oversized new files). A head
# lands only when all three are bound to its run and `success`.
VERDICT_CONTEXTS = (BUILD_CONTEXT, "bump-guard", "scope")
# The `build` description pr-build.yml's finalizer (`finalize_batch.py`) posts when the
# `sandboxed-build` job produced no build-state: its runner died or the job was cancelled or skipped
# before its report step ran. The report step always writes its own description, so this text is
# never a verdict on the source; a bound failed status carrying it is infrastructure.
INCOMPLETE_BUILD_DESCRIPTION = "sandboxed merge-group build did not complete"
LOG_GRACE_SECONDS = 600  # publication grace for a completed run's bound verdicts and failed-build log
# A source-candidate run's GitHub job and step phases are read at most this often while it runs, and
# once more when it completes; observation itself stays on `polling_interval_seconds`. Only the head's
# run is observed, and one read is a single jobs listing (one REST call with the GitHub App's
# installation token), so this costs at most one call a minute.
SOURCE_PHASES_INTERVAL_SECONDS = 60
DISPATCH_EVENT = "tauceti-bors-staging"
# How many of a batch's previous tips a CI dispatch names in `repair_outputs_revs`.
REPAIR_OUTPUTS_PREVIOUS_TIPS = 4
# How many of the most recently landed heads lend their published tips as fetch candidates.
REPAIR_OUTPUTS_LANDED_HEADS = 2
# Staging is dispatched only on a base with a published Lake cache (`Controller._base_cache_ready`).
# The base is covered by the revision map of itself or of a first-parent ancestor at most this many
# commits back whose difference from the base touches no Lake build input (`lake_build_input`).
BASE_CACHE_ANCESTORS = 20
# `batches.urgent` of an emergency batch, which dispatches without waiting for a published base: it
# is the means of publishing `main` again after an out-of-queue push.
URGENT_EMERGENCY = 2
# `batches.lane` of the control-file lane batch (`_lane_batch`): GitHub PRs whose own diff leaves the
# native path policy, staged ahead of the ordinary open batches while `control_lane_enabled` is on.
LANE_CONTROL = "control"
# The key of an input's `evidence` that records its control-file classification (`_classify_control_inputs`).
CONTROL_EVIDENCE = "control_files"
# `inputs.last_probe` of a GitHub input the lane could not classify yet (its head is unavailable).
CONTROL_UNCLASSIFIED = "control-lane: waits for its head to be classified"
MERGED_TITLE_PREFIX = "[Merged by Bors] - "  # what bors-ng writes when it closes a squash-landed member
COMMENT_MARKER = "tauceti-queue"

# Settings an operator can change at runtime (`set_setting`), with their initial values. The type of
# the initial value is the type a new value must have.
DEFAULT_SETTINGS: dict[str, Any] = {
    "insertion_age_window": 16,
    "open_batch_limit": 2,
    # Freeze oldest unsealed batches at this size until two active batches are sealed in total.
    # Zero disables successor sealing. Head creation never waits for this threshold.
    "sealed_successor_min_members": 100,
    "repair_concurrency": 1,
    # A background task may start only while fewer than this many total worker claims are live.
    # With repair_concurrency=3 and this limit=2, only head work can start the third worker.
    "background_start_limit": 2,
    "stall_interval_seconds": 900,
    "publication_enabled": False,
    "forgejo_admission_enabled": True,
    # Between the full Forgejo listings (once per approval-poll interval), list only the open
    # Forgejo PRs updated since the last listing, every reconcile pass, newest first.
    "forgejo_recent_listing": True,
    # A settled Forgejo PR whose listed facts have not changed is evaluated again this long after
    # its last attempt, for evidence neither the listings nor the comment feed show.
    "forgejo_recheck_seconds": 21600,
    # False holds every waiting Forgejo input out of insertion (it keeps its approval and age) while the
    # Forgejo source cannot be used; `evict_native` returns the ones already in batches. True resumes.
    "forgejo_placement_enabled": True,
    "claim_ttl_seconds": 900,
    "dispatch_timeout_seconds": 600,
    # An in-progress run counts as lost only when neither its jobs nor GitHub's direct read of the run
    # (`updated_at`) have moved for this long; until then a long build is a `stall` reason line.
    "lost_run_seconds": 14400,
    "infrastructure_retry_backoff_seconds": 60,
    "inference_failure_threshold": 3,
    "inference_hold_backoff_seconds": 60,
    "inference_hold_backoff_max_seconds": 3600,
    "outbox_retry_seconds": 30,
    "outbox_retry_max_seconds": 900,
    "polling_interval_seconds": 60,
    "approval_poll_interval_seconds": 900,
    # Admission's background GitHub reads stop while the queue App installation has at most this
    # many requests left (GraphQL: points), until the window resets; the rest is kept for landing,
    # dispatch and commands.
    "github_background_floor": 1000,
    # Decision 38 step 4: "off" keeps no admission evidence cache; "shadow" fills it from webhook
    # deliveries and full reads and compares its verdict with each real evaluation (status
    # `admission.evidence`), never changing a decision.
    "admission_evidence_mode": "off",
    # Decision 38 step 4, slice 2: take a GitHub PR's merge base from the local clone, when the
    # clone is complete and the merge base is unique there, instead of the compare API (one REST
    # call per evaluation). Otherwise, and while false, compare answers as before.
    "admission_local_merge_base": False,
    # Decision 38 step 4, slice 3: before an approved GitHub head joins a batch, re-read its
    # admission inputs (one GraphQL call) and hold the insertion unless they still match what
    # admission evaluated. False keeps today's local check only.
    "admission_insertion_check": False,
    "membership_label": "bors-staging",
    # Added to a landed member whose commit the queue modified (one with an edit summary in its landing
    # comment); created in the repository on first use. Empty disables it.
    "repaired_label": "repaired-in-queue",
    # Wall-clock bounds on one tick's work: insertion stops probing and the outbox stops draining once
    # the budget is spent, and the loop is woken to continue in the next tick. The probes' and the
    # drain's GitHub I/O runs without the controller lock (`ControllerLock`), so the worker endpoints
    # answer during either; the budgets bound how long one tick defers the rest of its pass.
    "insertion_probe_budget_seconds": 20,
    "outbox_drain_budget_seconds": 20,
    # Background preparation of open batches (`prepare` tasks): an open batch of at least
    # `prepare_min_members` members that has had no insertion for `prepare_quiet_seconds` and has not
    # been prepared at its current version is built and repaired on its own base, beside the head's
    # own tasks; such a batch gets a `pre_rebase` task only once it is prepared.
    "prepare_open_batches": True,
    "prepare_min_members": 20,
    "prepare_quiet_seconds": 600,
    # Replay the oldest open batch onto the head's tip once the head's chain is submitted and waits
    # for its CI verdict, so the next head seals from the result without a replay (`_pre_rebase`).
    "pre_rebase_enabled": True,
    # Build, repair and publish an open batch's usable pre-rebase on the head's tip (a `prepare` task
    # of the pre-rebased chain, `_prepare_pre_rebased`), so the head it seals as reaches CI with
    # published outputs for its exact tip. Applies to the batches own-base preparation applies to.
    "prepare_pre_rebased": True,
    # Labels by which an author opts a PR out of agent repair (case-insensitive): such a member is never
    # edited; when it would need an edit the agent empties it and the queue withdraws it as
    # `problem-anticipated` (`_anticipate`).
    "no_repair_labels": ["dont-repair-in-queue"],
    # A member an adopted repair reduces to an empty commit (and whose PR is not simply already on
    # `main`) is withdrawn at adoption: input cancelled as `empty-commit`, approval withdrawn, one
    # explanatory comment to its author (`_withdraw_member`). False defers the decision to landing,
    # where an empty final commit is rejected and its PR closed (`_enqueue_rejection_effects`).
    "withdraw_emptied_at_adoption": True,
    # Infrastructure failures of one head version re-dispatch with short backoff
    # (`infrastructure_retry_backoff_seconds`) this many times; then the queue backs off automatically,
    # `automatic_retry_seconds` doubling up to `automatic_retry_max_seconds` between re-dispatches,
    # indefinitely, with one direct message when the backoff starts and one on recovery. 0: no backoff.
    "infrastructure_failure_limit": 5,
    # The automatic retry delay of the infrastructure backoff: this long at first, doubling at each
    # further retry up to the maximum.
    "automatic_retry_seconds": 1800,
    "automatic_retry_max_seconds": 14400,
    # How long an escalation (an infrastructure backoff's start) stays among the status page's alerts.
    "escalation_alert_seconds": 86400,
    # Staging is dispatched only on a base whose exact Lake revision map (or that of an ancestor with
    # the same build inputs) is published at this public endpoint, the one `pr-build.yml` reads
    # (`LAKE_CACHE_REVISION_ENDPOINT_PUBLIC`). Empty disables the requirement. A base found
    # unpublished is probed again after `base_cache_recheck_seconds`; a wait of
    # `base_cache_alert_seconds` raises a status alert and one direct message (0: never).
    "base_cache_endpoint": "https://cache.taucetiproject.org/revisions",
    "base_cache_recheck_seconds": 120,
    "base_cache_alert_seconds": 7200,
    # The emergency lane (`bors emergency`, `POST /admin/emergency`): a repository admin's request that
    # an approved PR become the next head as a single-member batch (`_emergency`). Off: every request
    # is refused `disabled`; a request already accepted keeps running.
    "emergency_enabled": False,
    # With the emergency lane on, the emergency batch takes the head from a running ordinary head
    # (`_evict`) unless that head is about to land or publishing its final oleans. Off: it waits for
    # the running head to land.
    "emergency_evict": False,
    # The control-file lane (`_control_lane_active`, docs/queue/mixed-sources.md): with mixed sources
    # admitted under GitHub primary, a GitHub PR whose own diff leaves the native path policy is staged
    # in its own batch, which becomes the head ahead of every ordinary open batch, and is never repaired.
    # Off: every lane rule is off and a live lane batch is staged as an ordinary open batch.
    "control_lane_enabled": False,
}

# The actor of the values `seed_setting` writes from the configuration file's `[settings]`.
SEED_ACTOR = "configuration"

# How long `status()` waits for the controller lock before serving the last snapshot instead.
STATUS_LOCK_WAIT_SECONDS = 2.0
# How long one `repository_admin(login)` answer stands; served stale while GitHub cannot answer.
ADMIN_CACHE_SECONDS = 6 * 3600.0

INPUT_WAITING = "waiting"
INPUT_RESERVED = "reserved"
INPUT_INSERTED = "inserted"
INPUT_LANDED = "landed"
INPUT_CANCELLED = "cancelled"
MISSING_SOURCE_IN_FLIGHT = "missing-source-in-flight"  # an input waiting for a source or a newer canonical base
# The evidence key of a `missing-source` refusal's dependency fingerprint: what `main` and the batches
# introducing sources held for the refused gaps. The refused head is re-admitted when it changes.
MISSING_SOURCE_WATCH = "missing_source_watch"
# The cancellation reason of a `missing-source` refusal whose dependencies changed: admission reads
# the PR again and, if it would still admit the head, restores the input (`restore_missing_source`).
MISSING_SOURCE_RECHECK = "missing-source-recheck"
# The cancellation reason of a `no_repair` member the agent emptied: withdrawn until the PR it
# interacts with lands on `main` (then `needs-rebase`) or leaves the queue (then re-admitted).
PROBLEM_ANTICIPATED = "problem-anticipated"
# The cancellation reason of a member whose final commit is empty: withdrawn when an adopted repair
# empties it (`withdraw_emptied_at_adoption`), else rejected when its batch lands.
EMPTY_COMMIT = "empty-commit"
# How many escalations the controller keeps for the status page.
ESCALATIONS_KEPT = 50
# The most CI findings a withdrawal or rejection comment quotes.
COMMENT_FINDINGS = 8
NEEDS_REBASE_LABEL = "needs-rebase"
# How `repaired_label` is created when the repository lacks it.
REPAIRED_LABEL_COLOR = "d4c5f9"
REPAIRED_LABEL_DESCRIPTION = "The merge queue modified this contribution while integrating it; see the landing comment"
# `inputs.anticipated.stage` of a `problem-anticipated` input: `pending` while the interacting PR is
# followed, `unattributed` when the agent named none, then `needs-rebase`, `readmitted`,
# `readmit-refused` (the PR could not be re-admitted as it stands) or `superseded` (approved again).
ANTICIPATED_PENDING = "pending"
# The most diff lines a landing comment shows for an edited member; longer diffs link the commit.
EDIT_DIFF_LINES = 400

BATCH_OPEN = "open"
BATCH_HEAD = "head"
BATCH_LANDED = "landed"
BATCH_REMOVED = "removed"

TASK_OPEN = "open"
TASK_PRE_REBASE = "pre_rebase"  # the kind of an open batch's task: finish its replay onto the head's tip
TASK_SUBMITTED = "submitted"
TASK_SUPERSEDED = "superseded"

TASK_REPAIR = "repair"
TASK_REBASE_CONFLICT = "rebase_conflict"
TASK_PREPARE = "prepare"
# `claim_task` hands out the lowest value first, then the oldest task: the head's own tasks always
# come first, then the oldest open batch's pre-rebase onto the head, then the background preparation
# of an open batch.
TASK_PRIORITY = {TASK_REBASE_CONFLICT: 0, TASK_REPAIR: 0, TASK_PRE_REBASE: 1, TASK_PREPARE: 2}
# The kinds of an open batch's background tasks: at most one of them is open or claimed per batch.
BACKGROUND_TASKS = (TASK_PRE_REBASE, TASK_PREPARE)
BACKGROUND_OPERATIONS = tuple(f"task:{kind}" for kind in BACKGROUND_TASKS)
# Claims that rewrite a whole open batch, which insertion goes around.
BATCH_TASK_OPERATIONS = (f"task:{TASK_PREPARE}", f"task:{TASK_REBASE_CONFLICT}")
# How often a worker holding a `prepare` claim renews it (`Task.extra.renew_after`): a refused renewal
# is how the worker learns that the controller took the batch back (it sealed, or the head needs the
# worker), so the interval bounds how long a superseded preparation keeps running.
HANDOFF_RENEW_SECONDS = 15
# Resolutions of a `prepare` task that settle its batch version: no second task is created for it.
PREPARE_SETTLED = ("adopted", "prepared", "failed")
# Worker release reasons: `prepared_clean` (the batch built without errors, nothing to repair) and the
# failures after which a `prepare` task is not retried at the same batch version.
PREPARE_CLEAN_REASON = "prepared_clean"
PREPARE_FAILED_REASONS = ("agent_failed", "push_failed")
# The most errors a preparation left that a later `pre_rebase` task's diagnostics carry.
PREPARE_ERRORS_LIMIT = 100
# An open batch's pre-rebase (`pre_rebase_state`): a usable chain on `pre_rebase_onto` replayed by the
# controller or adopted from a `pre_rebase` task, or a conflict waiting for that task.
PRE_REBASE_CLEAN = "clean"
PRE_REBASE_ADOPTED = "adopted"
PRE_REBASE_CONFLICT = "conflict"
PRE_REBASE_USABLE = (PRE_REBASE_CLEAN, PRE_REBASE_ADOPTED)

OUTCOME_PENDING = "pending"
OUTCOME_PASS = "pass"
OUTCOME_SOURCE_FAILURE = "source-failure"
OUTCOME_INFRASTRUCTURE = "infrastructure"
OUTCOME_SUPERSEDED = "superseded"
# Run statuses GitHub reports for a run that has not finished: such a run is never re-dispatched over.
LIVE_RUN_STATUSES = ("queued", "in_progress", "waiting", "requested", "pending")
# The detail of an in-progress run declared lost after a direct read confirmed it stale. Such a run is
# still re-adopted while GitHub reports it live: pr-build.yml's concurrency group cancels the older
# staging run when a second one for the same candidate starts, so a duplicate dispatch kills it.
LOST_IN_PROGRESS = "lost-run: in_progress"

_PR_MENTION_RE = re.compile(r"(?:(?P<repo>[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+))?#(?P<number>\d+)")
_SOURCE_PR_MENTION_RE = re.compile(
    r"forgejo:https?://[^\s`<>]+@[1-9][0-9]*#[1-9][0-9]*"
    r"|[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+#[1-9][0-9]*"
)
# The subject of a squash commit bors-ng lands: `<title> (#<number>)`, with no `Source-PR` trailer.
_BORS_SUBJECT_RE = re.compile(r"\(#(?P<number>\d+)\)\s*$")
# What the agent appends to an emptied `no_repair` member's message (the repair prompt's "Do not edit").
_INTERACTING_PR_RE = re.compile(r"^\s*Interacting-PR:\s*`?(?P<reference>[^\s`]+)", re.M | re.I)
_INTERACTING_FILE_RE = re.compile(r"^\s*Interacting-File:\s*`?(?P<file>[^`\n]+?)`?\s*$", re.M | re.I)
_FINDING_RE = re.compile(r"^\s*Finding:\s*(?P<finding>\S.*?)\s*$", re.M | re.I)

EXTERNAL_HEAD_RUNNING = "running"
EXTERNAL_HEAD_LANDED = "landed"
EXTERNAL_HEAD_RELEASED = "released"


def lake_build_input(path: str) -> bool:
    """Whether a change to `path` can change a Lake input hash of the root package's published outputs.

    Lake hashes each module's source and imports, the toolchain, the package configuration (whose
    `leanOptions` reach every module) and, through the manifest, the dependencies' revisions. The
    root package's modules are `TauCeti.lean`, `TauCeti/**.lean` and the `lean_exe` roots under
    `scripts/`; every `.lean` file counts, which also covers nested packages, conservatively."""
    return path.endswith(".lean") or path in ("lakefile.toml", "lakefile.lean", "lake-manifest.json", "lean-toolchain")


def revision_map_published(url: str, *, timeout: float = 30.0) -> bool:
    """Whether `url` serves a Lake revision map: an anonymous GET, validated like `lake_cache_probe.probe`.
    A 404 is False; any other failure raises, which the caller treats as unpublished."""
    import urllib.error
    import urllib.request

    from tauceti_tools.assets.scripts.lake_cache_probe import parse_map

    from .github_app import ssl_context

    request = urllib.request.Request(url, headers={"User-Agent": "tauceti-queue"})
    try:
        with urllib.request.urlopen(request, timeout=timeout, context=ssl_context()) as response:
            raw = response.read()
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return False
        raise
    parse_map(raw)
    return True


LIVE_PROGRESS_SECONDS = 60
LIVE_PROGRESS_SETTING = "live-progress"
# A reporter clock further ahead than this is not believed at all.
LIVE_PROGRESS_SKEW_SECONDS = 300


def live_progress_at(report: dict[str, Any] | None, now: float) -> tuple[str | None, float | None]:
    """A live build report's raw counter-change time and when it counts as progress: never after
    the report itself or `now`. Counters come from candidate output: the stall report only."""
    if not report or report.get("completed_tasks") is None:
        return None, None
    try:
        raw = str(report["last_progress_at"])
        observed = datetime.fromisoformat(str(report["observed_at"]).replace("Z", "+00:00")).timestamp()
        advanced = datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
    except (KeyError, TypeError, ValueError):
        return None, None
    if max(advanced, observed) > now + LIVE_PROGRESS_SKEW_SECONDS:
        return None, None
    return raw, min(advanced, observed, now)


def current_step(phases: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The last running step of the recorded GitHub phases: its job, name, 1-based position among
    that job's steps and start time. Phases list each job followed by its `"<job>: <step>"` steps."""
    jobs: list[tuple[str, list[dict[str, Any]]]] = []
    for phase in phases:
        name = str(phase.get("name") or "")
        if jobs and name.startswith(jobs[-1][0] + ": "):
            jobs[-1][1].append(phase)
        else:
            jobs.append((name, []))
    for job, steps in reversed(jobs):
        for index in range(len(steps) - 1, -1, -1):
            if steps[index].get("status") == "in_progress":
                return {
                    "job": job,
                    "name": str(steps[index].get("name"))[len(job) + 2 :],
                    "index": index + 1,
                    "total": len(steps),
                    "started_at": steps[index].get("started_at"),
                }
    return None


# The phases of pr-build.yml that say a candidate run is publishing its final oleans (`publishing`):
# the `sandboxed-build` job's build step, then its handoff step or the `publish-merge-group-cache` job.
CANDIDATE_BUILD_JOB = "sandboxed-build"
CANDIDATE_BUILD_STEP = "Build exact candidate under bwrap"
CANDIDATE_HANDOFF_STEP = "Hand the merge-group oleans to the isolated publisher"
CANDIDATE_PUBLICATION_JOB = "publish-merge-group-cache"
# A phase conclusion that does not end a publication (anything else, a failure or a cancellation, does).
PUBLICATION_CONCLUSIONS = ("", "success", "skipped", "neutral")


def publishing(phases: list[dict[str, Any]]) -> bool:
    """The recorded GitHub phases of an unfinished run say it is publishing its final oleans: the build
    step concluded `success`, a publication phase (the handoff step, or the publication job or one of
    its steps) has started, and none of them concluded unsuccessfully. A publisher still waiting for a
    runner has not started. Phases are `"<job>"` and `"<job>: <step>"`; a job of a called workflow is
    named `"<caller> / <job>"`."""
    built = False
    started = False
    for phase in phases:
        name = str(phase.get("name") or "")
        job, _, step = name.partition(": ")
        jobs = job.split(" / ")
        build_job = jobs[-1] == CANDIDATE_BUILD_JOB
        if build_job and step.startswith(CANDIDATE_BUILD_STEP):
            built = phase.get("status") == "completed" and phase.get("detail") == "success"
            continue
        if not (CANDIDATE_PUBLICATION_JOB in jobs or (build_job and step.startswith(CANDIDATE_HANDOFF_STEP))):
            continue
        if phase.get("status") == "completed" and phase.get("detail") not in PUBLICATION_CONCLUSIONS:
            return False
        started = started or phase.get("started_at") is not None
    return built and started


def _finalizer_finding(finding: dict[str, Any]) -> bool:
    """An infrastructure finding about the `finalize-merge-group-build` job: it failed, or it was
    cancelled or timed out. Each says only that the `build` status may not have been published."""
    if finding.get("kind") != "infrastructure":
        return False
    if finding.get("reason") == "finalizer_failed":
        return True
    job = str(finding.get("job") or "").rsplit(" / ", 1)[-1]
    return job == "finalize-merge-group-build" and finding.get("reason") in ("cancelled", "timed_out")


class ControllerError(Exception):
    """A caller error (unknown repository, unknown setting, bad value); carries a stable `code`."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(detail or code)
        self.code = code
        self.detail = detail


class TaskError(ControllerError):
    """A task API error: unknown task or claim, expired or superseded claim, not authorized."""


class ControllerLock:
    """The controller lock: reentrant, with a priority lane for the worker endpoints.

    A priority acquirer (`priority()`: claim, renew, release, submit, inference reports) waits only
    for the current holder; any other acquirer also waits while a priority acquirer is waiting. So
    whenever the holder lets go, the worker endpoints go first. The tick lets go entirely around its
    GitHub I/O (`unlocked()`) and between units of work (`offer()`), so a worker request waits for at
    most one unit of the tick's work, not for the whole tick.
    """

    def __init__(self) -> None:
        mutex = threading.Lock()
        self._cond = threading.Condition(mutex)
        self._priority_cond = threading.Condition(mutex)
        self._owner: int | None = None
        self._depth = 0
        self._priority_waiting = 0

    def _take(self, priority: bool, deadline: float | None) -> bool:
        """Under `_cond`: wait until the lock is free (and, for an ordinary acquirer, no priority
        acquirer waits), then own it once. False when `deadline` passes first."""
        if priority:
            self._priority_waiting += 1
        condition = self._priority_cond if priority else self._cond
        try:
            while self._owner is not None or (not priority and self._priority_waiting):
                if deadline is None:
                    condition.wait()
                    continue
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                condition.wait(remaining)
            self._owner = threading.get_ident()
            self._depth = 1
            return True
        finally:
            if priority:
                self._priority_waiting -= 1
                if self._owner is None:
                    self._notify_next()

    def _notify_next(self) -> None:
        """Under the shared mutex: wake one eligible acquirer. Broadcasting to every webhook
        waiter makes draining a burst quadratic and can starve worker endpoints of CPU.
        """
        condition = self._priority_cond if self._priority_waiting else self._cond
        condition.notify(1)

    def acquire(self, blocking: bool = True, timeout: float = -1, *, priority: bool = False) -> bool:
        with self._cond:
            if self._owner == threading.get_ident():
                self._depth += 1
                return True
            if not blocking:
                timeout = 0
            deadline = None if timeout is None or timeout < 0 else time.monotonic() + timeout
            return self._take(priority, deadline)

    def release(self) -> None:
        with self._cond:
            if self._owner != threading.get_ident():
                raise RuntimeError("cannot release a controller lock this thread does not hold")
            self._depth -= 1
            if not self._depth:
                self._owner = None
                self._notify_next()

    def __enter__(self) -> ControllerLock:
        self.acquire()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.release()

    @contextlib.contextmanager
    def priority(self) -> Iterator[None]:
        """Hold the lock, ahead of every ordinary acquirer that is waiting."""
        self.acquire(priority=True)
        try:
            yield
        finally:
            self.release()

    def waiting(self) -> int:
        """The priority acquirers waiting for the lock now."""
        with self._cond:
            return self._priority_waiting

    def owned(self) -> bool:
        with self._cond:
            return self._owner == threading.get_ident()

    @contextlib.contextmanager
    def unlocked(self, *, priority: bool = False, reacquire=None) -> Iterator[None]:
        """Let go of every level this thread holds for the body, then take them back (ordinary by
        default, or at priority when requested). A no-op for a thread that
        does not hold the lock. The caller must not be inside a store transaction.

        An optional reacquire callback runs without the lock and must acquire one
        level itself; this supports timed priority acquisition with refreshed
        external evidence. On callback failure we still restore the original
        ownership/depth before propagating the error.
        """
        with self._cond:
            if self._owner != threading.get_ident():
                depth = 0
            else:
                depth, self._depth, self._owner = self._depth, 0, None
                self._notify_next()
        try:
            yield
        finally:
            if depth:
                try:
                    if reacquire is not None:
                        reacquire()
                        with self._cond:
                            if self._owner != threading.get_ident():
                                raise RuntimeError("reacquire callback did not take the controller lock")
                finally:
                    with self._cond:
                        if self._owner != threading.get_ident():
                            self._take(priority, None)
                        self._depth = depth

    def offer(self) -> None:
        """Between two units of a long holder's work: let the waiting priority requests run."""
        if self.waiting():
            with self.unlocked():
                pass


class Controller:
    def __init__(
        self,
        store: Store,
        github: SourcePort,
        clone: str,
        repository: str,
        *,
        revision: str,
        execution: ExecutionPort | None = None,
        candidate_ci=None,
        source_authority=None,
        mixed=None,
        canonical_branch: str | None = None,
        clock: Callable[[], float] = time.time,
        rng: random.Random | None = None,
        logger: logging.Logger | None = None,
        notifier: escalation.Notifier | None = None,
        revision_map_probe: Callable[[str], bool] | None = None,
        live_progress: Callable[[str, int], dict[str, Any] | None] | None = None,
    ) -> None:
        with store.transaction():
            if store.get_setting("queue:operating-mode") not in {None, "controller"}:
                raise ValueError("Stage 1 operator ledger cannot be opened by the autonomous controller")
            store.set_setting("queue:operating-mode", "controller")
        self.store = store
        self.admission = None  # primary policy intake, attached by the service in policy mode
        if store.get_setting("mixed:primary", "github") != "github" and mixed is None:
            raise ValueError("Forgejo primary cannot run without its explicit mixed-source configuration")
        self.source = mixed.port if mixed is not None and mixed.mode == "admit" else github
        self.mixed = mixed
        self.mixed_ci = None
        self.execution = execution if execution is not None else github
        self.candidate_ci = candidate_ci
        # The head run's public live build report (`build_progress.lookup`), read off the
        # reconciliation thread at most every LIVE_PROGRESS_SECONDS. Observational only: its counts
        # moving is progress for the stall report, never an input to a verdict.
        self.live_progress = live_progress
        # (run id, run attempt) -> {"checked_at", "raw", "progress_at"} for the head's run only.
        self._live: dict[tuple[str, int], dict[str, Any]] = {}
        self._live_reading = False
        self._live_failure: str | None = None
        self._live_lock = threading.Lock()
        # (attempt id, run id, run attempt) -> (time, run status) of the last phase read of a source run.
        self._source_phases_read: dict[tuple[Any, str, int], tuple[float, str]] = {}
        # The keys of `_source_phases_read` whose last read failed (`_eviction_observation` needs a fresh one).
        self._source_phases_unavailable: set[tuple[Any, str, int]] = set()
        self.source_authority = source_authority
        if source_authority is not None and (
            not isinstance(source_authority, NativeQueueAuthority)
            or candidate_ci is None
            or source_authority.source != candidate_ci.ledger.policy.source
        ):
            raise ValueError("native queue authority differs from its source CI binding")
        if candidate_ci is None and store.get_setting("source-ci:binding") is not None:
            raise ValueError("source-bound queue ledger requires its qualified CI coordinator")
        if candidate_ci is not None:
            if candidate_ci.ledger.policy.source.repository.forge.kind != "forgejo":
                raise ValueError("source CI currently requires the qualified Forgejo task protocol")
            if candidate_ci.ledger.store is not store or candidate_ci.ledger.clone != str(clone):
                raise ValueError("source CI ledger does not belong to this controller")
            if getattr(github, "repository_identity", None) != candidate_ci.ledger.policy.source:
                raise ValueError("source CI source port identity differs from its policy")
            if repository != candidate_ci.ledger.policy.source.repository.name:
                raise ValueError("controller repository differs from its qualified source")
            if not all(
                callable(getattr(github, name, None)) for name in ("retain_candidate", "verify_candidate_approval")
            ):
                raise ValueError("source CI needs qualified retention and fresh approval operations")
        self.github = self.source  # compatibility for source-authentication/service callers
        self.clone = clone
        self.repository = repository
        source_identity = getattr(self.source, "repository_identity", None)
        self.native_source = bool(source_identity and source_identity.repository.forge.kind == "forgejo")
        if self.native_source and candidate_ci is None:
            raise ValueError("Forgejo source requires qualified source CI; legacy GitHub execution is unavailable")
        self.provenance_repository = source_identity.repository.key if self.native_source else repository
        self.revision = revision
        self.clock = clock
        self.rng = rng or random.Random()
        self.log = logger or logging.getLogger("tauceti_queue.controller")
        self.started_at = clock()
        # `repository_admin(login)` per login, kept for `ADMIN_CACHE_SECONDS` and served stale while
        # GitHub cannot answer, so a quota outage neither refuses nor grants anything new; persisted
        # in the store's `identity_cache`, so a restart during the outage keeps the answers.
        self.admins = StaleCache(
            github.repository_admin,
            ttl=ADMIN_CACHE_SECONDS,
            clock=clock,
            logger=self.log,
            what="admin",
            store=store,
            namespace="admin",
            decode=bool,
        )
        # A co-located repair worker's login (`github_app.local_worker_login`): the server resolves it
        # only from a bearer signed with the App's private key, so it is a worker for claims and
        # submissions; the server's admin endpoints refuse it.
        local_login = getattr(github, "local_worker_login", None)
        self.local_worker_login = local_login() if callable(local_login) else None
        self._lock = ControllerLock()
        # Serializes ticks: the tick lets go of `_lock` around its GitHub I/O, so the lock alone no
        # longer keeps a second tick (or drain) from interleaving with the first.
        self._ticking = threading.RLock()
        # Renewals waiting for the lock: claim token -> when the request arrived. `_expire_claims`
        # spares a claim whose renewal arrived before it expired.
        self._renewing: dict[str, float] = {}
        self._renewal_waiters: dict[str, dict[object, float]] = {}
        self._renewing_guard = threading.Lock()
        self._wake = threading.Event()
        self._reasons: list[str] = []
        # The last status document and the monotonic time it was taken, served by `status()` when the
        # lock is held by a long tick.
        self._status_snapshot: tuple[float, dict[str, Any]] | None = None
        self.canonical_branch = canonical_branch or self._discover_canonical_branch()
        if candidate_ci is not None and (
            repository != candidate_ci.ledger.policy.source.repository.name
            or self.canonical_branch != candidate_ci.ledger.policy.base_branch
        ):
            raise ValueError("controller source repository or branch differs from its CI policy")
        self._canonical: str | None = self.store.get_setting("state:canonical_sha")
        # The canonical commit and active batch tips `_recheck_missing_sources` last looked at.
        self._missing_source_signature: tuple[Any, ...] | None = None
        # Labels this process has made sure exist in the repository (`_ensure_labels`).
        self._labels_ensured: set[tuple[str, str]] = set()
        # Where escalations are told to the operator besides the status page (`_notify`): the Zulip
        # bot `TAUCETI_QUEUE_ZULIP_CONFIG` configures, unless the caller passes one. A configuration
        # that cannot be read is reported among the status alerts rather than stopping the service.
        self.notifier_error = ""
        if notifier is None:
            try:
                notifier = escalation.notifier_from_environment()
            except (OSError, ValueError, TypeError) as error:
                self.notifier_error = f"{type(error).__name__}: {error}"[:300]
                self.log.warning("escalation notifier unavailable: %s", self.notifier_error)
        self.notifier = notifier
        # The service's public URL (configuration `public_url`), linked from notifications when set.
        self.public_url = ""
        # The CI recovery pass's stuck-head report (configuration `ci_recovery_report`), read for the
        # status and its one direct message per stuck period (`stuck_heads`). None: not reported.
        self.stuck_heads_report: str | None = None
        # Reads a Lake revision-map URL for the published-base requirement (`_base_cache_ready`). None
        # (the fixtures, native sources) leaves the requirement off; the service passes
        # `revision_map_published`. Per base: (covering revision or None, checked at, detail).
        self.revision_map_probe = revision_map_probe
        self._base_cache: dict[str, tuple[str | None, float, str]] = {}

    # --- settings ----------------------------------------------------------------------------------

    def setting(self, key: str) -> Any:
        if key not in DEFAULT_SETTINGS:
            raise ControllerError("unknown-setting", key)
        return self.store.get_setting(key, DEFAULT_SETTINGS[key])

    def effective_settings(self) -> dict[str, Any]:
        return {key: self.setting(key) for key in DEFAULT_SETTINGS}

    def set_setting(self, key: str, value: Any, actor: str) -> dict[str, Any]:
        """Change a runtime setting; applies to the next scheduling decision. Returns the effective settings.

        Writing the value a setting already has journals nothing and does not wake the loop; the
        store then only records the actor, when it held no value for the key or the value was a
        configuration seed, so an operator's explicit choice is not replaced by a later seed."""
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
            elif isinstance(expected, list):
                if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
                    raise ControllerError("bad-setting-value", f"{key} must be a list of strings")
            elif not isinstance(value, type(expected)):
                raise ControllerError("bad-setting-value", f"{key} must be a {type(expected).__name__}")
            if key == "admission_evidence_mode":
                from .admission_evidence import MODES

                if value not in MODES:
                    raise ControllerError("bad-setting-value", f"{key} must be one of {', '.join(MODES)}")
            now = self.clock()
            previous = self.setting(key)
            if value == previous:
                record = self.store.setting_record(key)
                if record is None or (record["actor"] == SEED_ACTOR and actor != SEED_ACTOR):
                    self.store.set_setting(key, value, actor, now)
                return self.effective_settings()
            with self.store.transaction():
                self.store.set_setting(key, value, actor, now)
                self.store.journal(now, "setting", key=key, value=value, actor=actor)
                if key == "publication_enabled" and value and not previous:
                    self._republish_retained_refs(now)
                if (
                    key == "forgejo_admission_enabled"
                    and value
                    and self.mixed is not None
                    and self.mixed.poller is not None
                ):
                    self.mixed.poller.admission_changed()
            self._wake.set()
            self._refresh_snapshot()
            return self.effective_settings()

    def seed_setting(self, key: str, value: Any, actor: str = SEED_ACTOR) -> bool:
        """Apply a configuration file's value of `key` as a seed: written when the store has no value
        for the key, or when the stored value was itself written by `actor` and differs; a value an
        operator set (any other actor) is kept. Returns whether the seed was written."""
        with self._lock:
            if key not in DEFAULT_SETTINGS:
                raise ControllerError("unknown-setting", key)
            record = self.store.setting_record(key)
            if record is not None and (record["actor"] != actor or record["value"] == value):
                return False
            self.set_setting(key, value, actor)
            return True

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
        self.source.fetch_refs(self.repository, [f"+refs/heads/{self.canonical_branch}:{self._canonical_ref()}"])
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
        from .mixed_identity import SourceNumber

        key = number.ref_key if isinstance(number, SourceNumber) else number
        return f"{chain.REF_NAMESPACE}/inputs/{key}/{sha}"

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
    def pre_rebase_ref(batch_id: int, onto: str) -> str:
        """The clone-only ref holding an open batch's chain replayed onto the head's tip `onto`."""
        return f"{chain.REF_NAMESPACE}/batches/{batch_id}/pre/{onto}"

    @staticmethod
    def pre_rebased_ref(batch_id: int, tip: str) -> str:
        """The published ref of an open batch's pre-rebased chain `tip`, which a worker preparing it fetches."""
        return f"{chain.REF_NAMESPACE}/batches/{batch_id}/pre-rebased/{tip}"

    @staticmethod
    def submission_ref(task_id: int, claim_id: int) -> str:
        return f"{chain.REF_NAMESPACE}/submissions/{task_id}/{claim_id}"

    def _fetch_head(self, number: int, sha: str) -> bool:
        """Bring a PR head into the clone: by its pull ref first, by SHA if the ref has moved on."""
        if chain.object_exists(self.clone, sha):
            return True
        from .mixed_identity import SourceNumber

        if isinstance(number, SourceNumber):
            if self.mixed is None or self.mixed.mode != "admit":
                return False
            return self.mixed.fetch_head(number, sha)
        probe = f"{chain.REF_NAMESPACE}/pulls/{number}"
        try:
            self.source.fetch_refs(self.repository, [f"+refs/pull/{number}/head:{probe}"])
        except Exception as error:  # noqa: BLE001 - the adapter's errors are reported, not propagated
            self.log.warning("fetch of refs/pull/%s/head failed: %s", number, error)
        if chain.object_exists(self.clone, sha):
            return True
        try:
            self.source.fetch_refs(self.repository, [sha])
        except Exception as error:  # noqa: BLE001
            self.log.warning("fetch of %s failed: %s", sha, error)
        return chain.object_exists(self.clone, sha)

    def _source_number(self, repo, number):
        if self.mixed is not None and repo == self.mixed.profile.forgejo.repository.key:
            return self.mixed.number(number)
        return number

    def _source_pr(self, number):
        from .mixed_identity import SourceNumber

        return number.source_pr if isinstance(number, SourceNumber) else f"{self.provenance_repository}#{number}"

    def _member_repository(self, number):
        from .mixed_identity import SourceNumber

        return (
            f"{number.source.repository.forge.key}/{number.source.repository.name}@{number.source.native_id}"
            if isinstance(number, SourceNumber)
            else self.provenance_repository
        )

    def _pull_url(self, number):
        from .mixed_identity import SourceNumber

        return (
            number.pull_url
            if isinstance(number, SourceNumber)
            else f"https://github.com/{self.repository}/pull/{number}"
        )

    def _check_repo(self, repo: str) -> None:
        if self.mixed is not None and repo == self.mixed.profile.forgejo.repository.key:
            if self.mixed.mode != "admit":
                raise ControllerError("forgejo-inert", "native queue admission is not enabled")
            return
        if repo != self.repository:
            raise ControllerError("unknown-repository", repo)

    # --- approvals ---------------------------------------------------------------------------------

    def _fetch_declaration_names(self, now: float) -> bool:
        """Poll/start at most two background reads, without delaying a reconciliation pass.

        Only this method mutates the store, while holding the controller lock; background
        threads only use the App adapter. Recheck accepted revision and active input state on
        completion, so late evidence cannot revive a cancelled input or follow a changed head.
        Unknown/missing producer evidence is final for this revision; transport exceptions have
        three attempts with backoff. Persisted attempts bound retries across service restarts.
        """
        if self.candidate_ci is not None or not hasattr(self.execution, "declaration_names"):
            return False
        if not hasattr(self, "_declaration_fetches"):
            self._declaration_fetches: dict[str, concurrent.futures.Future] = {}
        changed = False
        active = (INPUT_WAITING, INPUT_RESERVED, INPUT_INSERTED)
        for head, future in list(self._declaration_fetches.items()):
            if not future.done():
                continue
            del self._declaration_fetches[head]
            rows = [
                r
                for r in self.store.rows("inputs", {"repository": self.repository, "head_sha": head})
                if r["state"] in active
            ]
            try:
                record = future.result()
                if not isinstance(record, dict) or record.get("state") not in (
                    "known",
                    "empty",
                    "unknown",
                    "incomplete",
                ):
                    record = declaration_names.unknown("invalid_record")
                if record.get("state") in ("known", "empty") and declaration_names.accepted(record, head) is None:
                    record = declaration_names.unknown("invalid_record")
            except Exception as error:  # bounded retry of transport/provider failure only
                for row in rows:
                    attempts = row["declaration_fetch_attempts"]
                    if attempts < 3:
                        self.store.update(
                            "inputs", {"id": row["id"]}, declaration_retry_at=now + 60 * 2 ** (attempts - 1)
                        )
                    else:
                        record = declaration_names.unknown("fetch_failed")
                        self.store.update("inputs", {"id": row["id"]}, declaration_names=dumps(record))
                        changed = True
                    self.store.journal(
                        now,
                        "declaration-names-fetch-failed",
                        input_id=row["id"],
                        number=row["number"],
                        head=head,
                        attempt=attempts,
                        error=str(error)[:500],
                    )
                continue
            for row in rows:
                self.store.update("inputs", {"id": row["id"]}, declaration_names=dumps(record), declaration_retry_at=0)
                self.store.journal(
                    now,
                    "declaration-names",
                    input_id=row["id"],
                    number=row["number"],
                    head=head,
                    state=record["state"],
                    reason=record.get("reason"),
                    names=sum(len(n) for n in record.get("added", {}).values()),
                )
                changed = True
        head_batch = self._head()
        rows = [
            r
            for r in self.store.rows("inputs", {"repository": self.repository})
            if r["state"] in active and not loads(r["declaration_names"], {}) and r["declaration_retry_at"] <= now
        ]
        rows.sort(key=lambda r: (r["batch_id"] != (head_batch or {}).get("id"), r["approved_at"], r["id"]))
        for row in rows:
            head = row["head_sha"]
            if len(self._declaration_fetches) >= 2:
                break
            if head in self._declaration_fetches:
                continue
            # A process may have stopped mid-read on its third attempt. That remains unknown,
            # rather than starting another request forever after each restart.
            if row["declaration_fetch_attempts"] >= 3:
                self.store.update(
                    "inputs",
                    {"id": row["id"]},
                    declaration_names=dumps(declaration_names.unknown("fetch_attempts_exhausted")),
                )
                changed = True
                continue
            for matching in rows:
                if matching["head_sha"] == head:
                    self.store.update(
                        "inputs",
                        {"id": matching["id"]},
                        declaration_fetch_attempts=matching["declaration_fetch_attempts"] + 1,
                    )
            if not hasattr(self, "_declaration_pool"):
                self._declaration_pool = concurrent.futures.ThreadPoolExecutor(
                    max_workers=2, thread_name_prefix="queue-declarations"
                )
            self._declaration_fetches[head] = self._declaration_pool.submit(
                self.execution.declaration_names, self.repository, head
            )
            self._declaration_fetches[head].add_done_callback(lambda _future: self._wake.set())
        return changed

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
            pr = self._source_number(repo, pr)
            now = self.clock()
            existing = self._input_for(pr)
            if existing is not None:
                self._admin_reapproval(existing, head_sha, evidence, now)
                if existing["head_sha"] == head_sha and existing["state"] in (INPUT_WAITING, INPUT_RESERVED):
                    changed = {}
                    if priority != existing["priority"]:
                        changed["priority"] = priority
                    if changed:
                        self.store.update("inputs", {"id": existing["id"]}, **changed)
                    if self.admission is not None and (evidence or {}).get("kind") != "admission_policy":
                        self.admission.accept_manual(pr, head_sha)
                    return {"accepted": True, "reason": "already-approved", "input": self._input_for(pr)}
                if existing["state"] == INPUT_INSERTED:
                    return {"accepted": False, "reason": "member", "input": existing}
            if isinstance(pr, SourceNumber) and not self.setting("forgejo_admission_enabled"):
                return {"accepted": False, "reason": "admission-paused", "input": None}
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
            no_repair = self._carries_no_repair(getattr(pull, "labels", None))
            ref = self.input_ref(pr, head_sha)
            with self.store.transaction():
                if existing is not None and existing["state"] in (INPUT_WAITING, INPUT_RESERVED):
                    # A validated new approval supersedes the old waiting revision, preserving terminal history.
                    self.store.update(
                        "inputs", {"id": existing["id"]}, state=INPUT_CANCELLED, reason="superseded-by-new-approval"
                    )
                self._retain(ref, head_sha)
                input_id = self.store.insert(
                    "inputs",
                    repository=self.repository,
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
                    no_repair=int(no_repair),
                )
                if self.admission is not None and (evidence or {}).get("kind") != "admission_policy":
                    self.admission.accept_manual(pr, head_sha)
                self.store.journal(
                    now,
                    "approved",
                    number=pr,
                    head=head_sha,
                    reviewer=reviewer,
                    input_id=input_id,
                    **({"no_repair": True} if no_repair else {}),
                )
                if no_repair:
                    self._count("no_repair_opt_outs")
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
            pull = self.source.fetch_pr(repo, pr)
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

    def _no_repair_labels(self) -> set[str]:
        return {str(label).lower() for label in self.setting("no_repair_labels") or ()}

    def _carries_no_repair(self, labels: Any) -> bool:
        """Whether a PR's labels include one of `no_repair_labels` (case-insensitive)."""
        return bool({str(label).lower() for label in labels or ()} & self._no_repair_labels())

    def input_for(self, repo: str, pr: int) -> dict[str, Any] | None:
        """The PR's newest live input row, or its newest terminal row when none is live, or None."""
        with self._lock:
            self._check_repo(repo)
            pr = self._source_number(repo, pr)
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

    def unapprove(self, repo: str, pr: int, actor: str = "", *, head_sha: str = "") -> dict[str, Any]:
        """`bors r-`: a waiting input is withdrawn; a batch member is not (membership is fixed)."""
        return self._withdraw(repo, pr, "unapproved", actor, head_sha=head_sha)

    def cancel(self, repo: str, pr: int, actor: str = "", *, head_sha: str = "") -> dict[str, Any]:
        """`bors cancel`: the same as `unapprove`; the head is never cancelled."""
        return self._withdraw(repo, pr, "cancelled", actor, head_sha=head_sha)

    def _withdraw(self, repo: str, pr: int, reason: str, actor: str, *, head_sha: str = "") -> dict[str, Any]:
        with self._lock:
            self._check_repo(repo)
            pr = self._source_number(repo, pr)
            now = self.clock()
            current = self._input_for(pr)
            if (
                self.admission is not None
                and reason in ("unapproved", "cancelled")
                and (current is None or current["state"] != INPUT_INSERTED)
            ):
                with self.store.transaction():
                    self.admission.withdraw(pr, head_sha or (current or {}).get("head_sha", ""))
                    self._drop_missing_source(current, reason, now)
                    if current is not None and current["state"] in (INPUT_WAITING, INPUT_RESERVED):
                        self.store.update("inputs", {"id": current["id"]}, state=INPUT_CANCELLED, reason=reason)
                        self.store.journal(now, "withdrawn", number=pr, reason=reason, actor=actor)
                        self._refresh_snapshot()
                        return {"withdrawn": True, "reason": reason}
            if current is None or current["state"] in (INPUT_LANDED, INPUT_CANCELLED):
                self._drop_missing_source(current, reason, now)
                return {"withdrawn": False, "reason": "not-waiting"}
            if current["state"] == INPUT_INSERTED:
                self.store.journal(now, "withdrawal-refused", number=pr, reason=reason, actor=actor)
                return {"withdrawn": False, "reason": "member"}
            with self.store.transaction():
                self.store.update("inputs", {"id": current["id"]}, state=INPUT_CANCELLED, reason=reason)
                self.store.journal(now, "withdrawn", number=pr, reason=reason, actor=actor)
            self._refresh_snapshot()
            return {"withdrawn": True, "reason": reason}

    def _drop_missing_source(self, current: dict[str, Any] | None, reason: str, now: float) -> None:
        """A withdrawal of a PR its `missing-source` refusal left cancelled: the refusal is no longer the
        last word, so `_recheck_missing_sources` must not re-admit it."""
        if (
            current is not None
            and current["state"] == INPUT_CANCELLED
            and current["reason"] in ("missing-source", MISSING_SOURCE_RECHECK)
        ):
            self.store.update("inputs", {"id": current["id"]}, reason=reason)
            self.store.journal(now, "missing-source-dropped", number=current["number"], reason=reason)

    def set_priority(self, repo: str, pr: int, priority: int) -> dict[str, Any]:
        with self._lock:
            self._check_repo(repo)
            pr = self._source_number(repo, pr)
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
            pr = self._source_number(repo, pr)
            now = self.clock()
            current = self._input_for(pr)
            if current is None or current["state"] in (INPUT_LANDED, INPUT_CANCELLED):
                return {"urgent": False, "reason": "not-queued"}
            with self.store.transaction():
                # Never lowers an emergency (`URGENT_EMERGENCY`) request or batch.
                self.store.update("inputs", {"id": current["id"]}, urgent=max(current["urgent"], 1))
                if current["state"] == INPUT_INSERTED and current["batch_id"] is not None:
                    batch = self.store.row("batches", {"id": current["batch_id"]}) or {"urgent": 0}
                    self.store.update("batches", {"id": current["batch_id"]}, urgent=max(batch["urgent"], 1))
                self.store.journal(now, "urgent", number=pr, actor=actor, state=current["state"])
            self._wake.set()
            self._refresh_snapshot()
            return {"urgent": True, "state": current["state"]}

    def _admin_reapproval(
        self, existing: dict[str, Any], head_sha: str, evidence: dict[str, Any] | None, now: float
    ) -> None:
        """An admin's approval of the live input's own accepted head, recorded beside the original evidence.

        `approve` does not replace a live input's evidence (the PR keeps its admission age, and an inserted
        member is never re-approved), so an admin's `r+` of a PR first approved by someone else would
        otherwise leave no admin scope. The new authorization is stored as its own record,
        `evidence.admin_reapproval`; the original evidence, `approved_at`, state and batch are untouched.
        """
        evidence = evidence or {}
        if (
            existing["state"] not in (INPUT_WAITING, INPUT_RESERVED, INPUT_INSERTED)
            or existing["head_sha"] != head_sha
            or not evidence.get("admin")
            or not evidence.get("actor_id")
            or not evidence.get("merge_base_sha")
        ):
            return
        stored = loads(existing["evidence"], {})
        record = {
            key: evidence.get(key)
            for key in (
                "actor_login",
                "actor_id",
                "comment_id",
                "comment_url",
                "created_at",
                "head_sha",
                "merge_base_sha",
                "command",
            )
        }
        same_as_original = all(stored.get(key) == record[key] for key in ("actor_id", "comment_id", "created_at"))
        if stored.get("admin_reapproval") == record or same_as_original:
            return  # the same approval seen again (a poll, a redelivery)
        stored["admin_reapproval"] = record
        with self.store.transaction():
            self.store.update("inputs", {"id": existing["id"]}, evidence=dumps(stored))
            self.store.journal(
                now,
                "admin-reapproved",
                existing["batch_id"] if existing["state"] == INPUT_INSERTED else None,
                number=existing["number"],
                head=head_sha,
                actor=evidence.get("actor_login"),
                state=existing["state"],
            )

    def request_emergency(
        self, repo: str, pr: int, actor: str, *, surface: str, reason: str = "", live_head: str | None = None
    ) -> dict[str, Any]:
        """A repository admin's request that the PR land next through the emergency lane (`_emergency`).

        The caller has checked that `actor` is a repository admin. The flag is never an approval: the PR
        needs a live input (waiting, reserved or inserted) whose head is the live PR head, read from the
        source unless `live_head` is given. Returns `{"accepted": bool, "reason": ..., "behind": PR | None}`;
        refusals are `disabled`, `not-approved`, `head-moved`, `fetch-failed: ...`, `forgejo-inert` and
        `already-requested`. Every request and refusal is journaled.
        """
        self._check_repo(repo)
        number = self._source_number(repo, pr)
        if live_head is None and self.setting("emergency_enabled"):
            try:
                live_head = self.source.fetch_pr(repo, number).head_sha
            except Exception as error:  # noqa: BLE001 - the refusal names the failure
                return self._refuse_emergency(number, actor, surface, f"fetch-failed: {error}"[:200])
        with self._lock:
            now = self.clock()
            if not self.setting("emergency_enabled"):
                return self._refuse_emergency(number, actor, surface, "disabled")
            current = self._input_for(number)
            if current is None or current["state"] not in (INPUT_WAITING, INPUT_RESERVED, INPUT_INSERTED):
                return self._refuse_emergency(number, actor, surface, "not-approved")
            if current["head_sha"] != live_head:
                return self._refuse_emergency(number, actor, surface, "head-moved")
            if isinstance(number, SourceNumber) and (self.mixed is None or self.mixed.mode != "admit"):
                return self._refuse_emergency(number, actor, surface, "forgejo-inert")
            if self._emergency_requested(current):
                return {"accepted": False, "reason": "already-requested", "behind": self._emergency_behind(current)}
            evidence = loads(current["evidence"], {})
            evidence["emergency"] = {"actor": actor, "requested_at": now, "surface": surface, "reason": reason}
            with self.store.transaction():
                # Only the input is flagged: an ordinary batch never becomes an emergency batch.
                self.store.update("inputs", {"id": current["id"]}, urgent=URGENT_EMERGENCY, evidence=dumps(evidence))
                if self.admission is not None:
                    # Its batch needs admission current at this head: evaluate it again now, ahead of
                    # all other pending admission work (`AdmissionState.next_due`).
                    self.admission.invalidate(number, head=current["head_sha"], urgent=True)
                self.store.journal(
                    now,
                    "emergency-requested",
                    number=number,
                    head=current["head_sha"],
                    actor=actor,
                    surface=surface,
                    reason=reason,
                    state=current["state"],
                )
            behind = self._emergency_behind(self.store.row("inputs", {"id": current["id"]}) or current)
            self._wake.set()
            self._refresh_snapshot()
            return {"accepted": True, "reason": "accepted", "behind": behind, "state": current["state"]}

    def _refuse_emergency(self, number: Any, actor: str, surface: str, reason: str) -> dict[str, Any]:
        with self._lock:
            self.store.journal(
                self.clock(), "emergency-refused", number=number, actor=actor, surface=surface, reason=reason
            )
        return {"accepted": False, "reason": reason, "behind": None}

    def cancel_emergency(self, repo: str, pr: int, actor: str, *, surface: str) -> dict[str, Any]:
        """Withdraw an emergency request (`bors emergency-`, `POST /admin/emergency-cancel`).

        A built emergency batch ends through `_end_emergency` (its member back where it was, its tasks,
        claims and runs retired), unless it is about to land: a pending `promote` outbox entry or a passed
        current attempt refuses the cancellation `promotion-pending`. A request with no batch yet is just
        cleared. The PR's approval is never withdrawn."""
        self._check_repo(repo)
        number = self._source_number(repo, pr)
        with self._lock:
            now = self.clock()
            current = self._input_for(number)
            batch = self._emergency_batch()
            if batch is not None and current is not None and self._emergency_input(batch) == current["id"]:
                attempt = self._current_attempt(batch)
                promoting = self.store.query(
                    "SELECT 1 FROM outbox WHERE kind='promote' AND state='pending' AND key LIKE ? LIMIT 1",
                    (f"promote:{batch['id']}:%",),
                )
                if promoting or (attempt is not None and attempt["outcome"] == OUTCOME_PASS):
                    self.store.journal(
                        now,
                        "emergency-cancel-refused",
                        batch["id"],
                        number=number,
                        actor=actor,
                        reason="promotion-pending",
                    )
                    return {"cancelled": False, "reason": "promotion-pending", "batch_id": batch["id"]}
                self._end_emergency(batch, "cancelled", now, actor=actor)
                self._refresh_snapshot()
                return {"cancelled": True, "reason": "cancelled", "batch_id": batch["id"]}
            if current is None or not self._emergency_requested(current):
                return {"cancelled": False, "reason": "not-requested", "batch_id": None}
            with self.store.transaction():
                self._clear_emergency_request(current)
                self.store.journal(
                    now, "emergency-ended", number=number, cause="cancelled", actor=actor, surface=surface
                )
            self._wake.set()
            self._refresh_snapshot()
            return {"cancelled": True, "reason": "cancelled", "batch_id": None}

    def _input_for(self, pr: int) -> dict[str, Any] | None:
        """The PR's newest live input, otherwise its newest terminal input."""
        from .mixed_identity import source_record

        rows = self.store.rows(
            "inputs",
            {"repository": self.repository, "number": int(pr), "source_identity": source_record(pr)},
            order="id DESC",
        )
        for row in rows:
            if row["state"] in (INPUT_WAITING, INPUT_RESERVED, INPUT_INSERTED):
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
        raw_number = payload.get("number") or pull.get("number") or 0
        number = raw_number if isinstance(raw_number, SourceNumber) else int(raw_number)
        if not number:
            return
        if action == "closed" and (pull.get("merged") or str(pull.get("title") or "").startswith(MERGED_TITLE_PREFIX)):
            self._external_member_landed({number}, "closed-by-bors", now)
        current = self._input_for(number)
        if current is None:
            return
        if action in ("labeled", "unlabeled"):
            self._blocking_label_event(current, payload, now)
            if self._no_repair_event(current, payload, now):
                return
        if current["state"] == INPUT_INSERTED:
            # Scenario 21: closing, relabelling, re-reviewing a member changes nothing; the content lands.
            self.store.journal(now, "member-event-ignored", number=number, action=action, batch_id=current["batch_id"])
            return
        if current["state"] == INPUT_CANCELLED and current["reason"] in ("missing-source", MISSING_SOURCE_RECHECK):
            # A refusal `_recheck_missing_sources` would re-admit no longer stands for the PR as it is.
            moved = action == "synchronize" and (pull.get("head") or {}).get("sha") not in (None, current["head_sha"])
            reason = {"closed": "closed", "converted_to_draft": "draft"}.get(action, "head-changed" if moved else "")
            if reason:
                self.store.update("inputs", {"id": current["id"]}, reason=reason)
                self.store.journal(now, "missing-source-dropped", number=number, reason=reason)
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

    def _blocking_label_event(self, current: dict[str, Any], payload: dict[str, Any], now: float) -> None:
        """A `labeled`/`unlabeled` delivery refreshes a live input's `blocked_labels`: from the delivery's
        label list when it has one, else by the one label it names. A delivery for another head is
        ignored. Deliveries can arrive out of order; with admission, the delivery also has the PR
        evaluated again before it can be placed, and that read's labels are the ones that stand."""
        if current["state"] not in (INPUT_WAITING, INPUT_RESERVED, INPUT_INSERTED):
            return
        pull = payload.get("pull_request") or {}
        if (pull.get("head") or {}).get("sha") not in (None, current["head_sha"]):
            return
        if isinstance(pull.get("labels"), list):
            labels = {str(label.get("name") if isinstance(label, dict) else label) for label in pull["labels"]}
        else:
            name = str((payload.get("label") or {}).get("name") or "").lower()
            labels = set(loads(current["blocked_labels"], []))
            if payload.get("action") == "labeled":
                labels.add(name)
            else:
                labels.discard(name)
        self.note_blocking_labels(current, labels, now)

    def note_blocking_labels(self, current: dict[str, Any], labels: Any, now: float | None = None) -> None:
        """Record which blocking labels (`BLOCKING_LABELS`) the PR of a live input carries.

        `approve` refuses these labels; one added after approval keeps a waiting input out of every
        batch (ordinary, control lane and emergency) while it stays, and the input keeps its state, age
        and approval, so removing the label is all it takes to place it again. An inserted member stays
        in its batch (membership is fixed); its labels are recorded so that, if the batch sends it back
        to waiting, the hold applies. Called for label deliveries and by admission and eligibility with
        the labels of their fresh read, so a missed delivery is corrected there."""
        blocked = sorted({str(label).lower() for label in labels or ()} & BLOCKING_LABELS)
        with self._lock:
            current = self.store.row("inputs", {"id": current["id"]})
            if current is None or current["state"] not in (INPUT_WAITING, INPUT_RESERVED, INPUT_INSERTED):
                return
            if blocked == loads(current["blocked_labels"], []):
                return
            now = self.clock() if now is None else now
            with self.store.transaction():
                self.store.update("inputs", {"id": current["id"]}, blocked_labels=dumps(blocked))
                self.store.journal(
                    now,
                    "blocked-label" if blocked else "blocked-label-cleared",
                    number=current["number"],
                    head=current["head_sha"],
                    labels=blocked,
                )
            self._wake.set()
            self._refresh_snapshot()

    @staticmethod
    def _label_blocked(row: dict[str, Any]) -> bool:
        return bool(loads(row["blocked_labels"], []))

    def _no_repair_event(self, current: dict[str, Any], payload: dict[str, Any], now: float) -> bool:
        """A `labeled`/`unlabeled` event that changes the PR's `no_repair` opt-out, applied to a waiting
        input and to an inserted member's record (so the next task's prompt and the submission check see
        it); True when the event concerned a `no_repair_labels` label. A withdrawn member is not changed:
        its commit stays empty until its head lands."""
        if current["state"] not in (INPUT_WAITING, INPUT_RESERVED, INPUT_INSERTED):
            return False
        names = self._no_repair_labels()
        changed = str((payload.get("label") or {}).get("name") or "").lower()
        pull = payload.get("pull_request") or {}
        if changed not in names:
            return False
        if isinstance(pull.get("labels"), list):
            labels = [label.get("name") if isinstance(label, dict) else label for label in pull["labels"]]
            no_repair = self._carries_no_repair(labels)
        else:
            no_repair = payload.get("action") == "labeled"
        if not no_repair and self._control_input(current):
            return True  # a control-file change is never repaired, whatever its labels say
        if no_repair == bool(current["no_repair"]):
            return True
        with self.store.transaction():
            self.store.update("inputs", {"id": current["id"]}, no_repair=int(no_repair))
            if current["state"] == INPUT_INSERTED:
                self.store.update(
                    "members",
                    {"input_id": current["id"], "batch_id": current["batch_id"], "withdrawn": 0},
                    no_repair=int(no_repair),
                )
            if no_repair:
                self._count("no_repair_opt_outs")
            self.store.journal(
                now,
                "no-repair",
                current["batch_id"] if current["state"] == INPUT_INSERTED else None,
                number=current["number"],
                no_repair=no_repair,
                state=current["state"],
            )
        return True

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
        with self._lock.priority():
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
        if self.candidate_ci is not None:
            return None  # Native authority never uses a cached GitHub login.
        if login and login == self.local_worker_login:
            return True  # The server resolves this login only from a bearer signed with the App's key.
        try:
            return bool(self.admins.get(login, login))
        except GitHubUnavailable as error:
            self.log.warning("admin check for %s failed with nothing cached: %s", login, error)
            return None

    def _source_request_access(self, evidence, *, claim=None):
        """Validate a native request under the lock without performing I/O."""
        if self.source_authority is None:
            raise PermissionError("native source authority is required")
        access = self.source_authority.validate(evidence)
        self.source_authority.require_admin(access)
        actor = {
            "origin": access.actor.principal.forge.key,
            "id": access.actor.principal.native_id,
            "login": access.actor.login,
        }
        if claim is not None:
            self._check_source_claim_owner(actor, claim)
        return access, actor

    @staticmethod
    def _check_source_claim_owner(actor, claim):
        owner = loads(claim.get("source_actor"), {})
        if not isinstance(owner, dict) or (owner.get("origin"), owner.get("id")) != (actor["origin"], actor["id"]):
            raise PermissionError("native source caller does not own this claim")

    def _refresh_source_access(self, evidence, refresh, *, claim=None):
        """Refresh outside the controller lock; callers must recheck their state."""
        if not self._lock.owned():
            raise RuntimeError("native authority refresh requires controller lock ownership")
        if refresh is None:
            self._source_request_access(evidence, claim=claim)
            return evidence
        actor = evidence.access.actor  # Validated at this operation's entry.
        fresh = None

        def acquire_fresh():
            nonlocal fresh
            for _ in range(3):
                fresh = refresh()
                try:
                    access, _ = self._source_request_access(fresh, claim=claim)
                except SourceAuthorityUnavailable:
                    continue
                if access.actor != actor:
                    raise PermissionError("native source caller changed while refreshing authority")
                remaining = max(0, 15 - (self.source_authority.clock() - fresh.checked_at))
                if not self._lock.acquire(priority=True, timeout=remaining):
                    continue
                try:
                    self._source_request_access(fresh, claim=claim)
                except SourceAuthorityUnavailable:
                    self._lock.release()
                    continue
                return
            raise SourceAuthorityUnavailable("controller stayed busy beyond fresh native authority")

        with self._lock.unlocked(priority=True, reacquire=acquire_fresh):
            pass
        return fresh

    def _submission_task_current(self, task, batch):
        if task["kind"] == TASK_PRE_REBASE:
            return self._pre_rebase_task_current(task, batch)
        if self._pre_rebased_prepare(task):
            return self._pre_rebased_prepare_current(task, batch)
        return (
            (
                task["state"] == TASK_OPEN
                and batch["version"] == task["batch_version"]
                and batch["state"] == (BATCH_OPEN if task["kind"] == TASK_PREPARE else BATCH_HEAD)
            )
            or (task["kind"] != TASK_PREPARE and self._evicted_task_current(task, batch))
            or self._open_rebase_current(task, batch)
        )

    def claim_task(
        self, worker_identity: str, github_login: str, *, source_access=None, expected_binding=None, mixed_capable=False
    ) -> Task | None:
        """Hand an open task to an authorized worker under a renewable claim, within `repair_concurrency`.

        `repair_concurrency` bounds the live task claims in total. Within it, tasks are offered in
        two tiers by the role of their batch, each by kind priority then age (`TASK_PRIORITY`): first
        the head's task (`repair` or `rebase_conflict`); then an open batch's background task (a
        retained task of a batch the emergency batch took the head from, `pre_rebase`, then
        `prepare`). Each batch has at most one live worker claim, including when its task changes.
        Claims on open batches number at most `repair_concurrency - 1`. Starting background work also
        requires fewer than `background_start_limit` total live claims; only head work may increase
        the active total beyond that limit. Head work never waits behind background work: while no
        claim on the head is live, the head's task is offered even when the claims already number
        `repair_concurrency` (only an evicted batch's retained claim can make them so). With two
        workers one repairs the head while the other prepares or pre-rebases the next batch; a worker
        waits when no distinct batch needs work.

        The admin check (a cached GitHub read) runs before the lock is taken, and the claim takes the
        lock's priority lane, so a claim answers while a tick is running.
        """
        if expected_binding is not None and self.candidate_ci is None:
            raise TaskError("not-authorized", "qualified task binding requires a native source controller")
        admin = self._repository_admin(github_login) if self.candidate_ci is None else None
        with self._lock.priority():
            actor = {}
            if self.candidate_ci is not None:
                if expected_binding is not None and expected_binding != TaskBinding.from_policy(
                    self.candidate_ci.ledger.policy, queue_epoch=self.candidate_ci.ledger.epoch
                ):
                    raise TaskError("not-authorized", "requested native task policy changed")
                _access, actor = self._source_request_access(source_access)
                github_login = ""  # Native identities never populate the legacy login field.
                admin = True
            now = self.clock()
            self._expire_claims(now)
            if not admin:
                reason = "not-admin" if admin is False else "github-unavailable"
                self.store.journal(now, "claim-refused", worker=worker_identity, login=github_login, reason=reason)
                return None
            concurrency = int(self.setting("repair_concurrency"))
            claimable = self._claimable_tasks()
            if not mixed_capable:
                claimable = [
                    pair
                    for pair in claimable
                    if loads(pair[0]["task_binding"], {}).get("schema") != "tauceti-queue.mixed-task/v1"
                ]
            active = self._active_task_claims()
            if len(active) >= concurrency:
                head = self._head() if concurrency > 0 else None
                head_work = [pair for pair in claimable if head is not None and pair[1]["id"] == head["id"]]
                if not head_work or any(claim["batch_id"] == head["id"] for claim in active):
                    if head_work:
                        self._count("claim_collisions")
                        self.store.journal(
                            now,
                            "claim-refused",
                            worker=worker_identity,
                            reason="concurrency",
                            task_id=head_work[0][0]["id"],
                        )
                    return None
                claimable = head_work
            chosen = self._choose_task(claimable, concurrency)
            if chosen is None:
                return None
            task, batch = chosen
            token = secrets.token_hex(16)
            with self.store.transaction():
                claim_id = self.store.insert(
                    "claims",
                    batch_id=batch["id"],
                    batch_version=batch["version"],
                    operation=f"task:{task['kind']}",
                    worker=worker_identity,
                    github_login=github_login,
                    source_actor=dumps(actor),
                    token=token,
                    starting_tip=batch["tip"],
                    task_id=task["id"],
                    created_at=now,
                    expires_at=now + int(self.setting("claim_ttl_seconds")),
                )
                self.store.update("tasks", {"id": task["id"]}, claims=task["claims"] + 1)
                self.store.journal(
                    now,
                    "claimed",
                    batch["id"],
                    task_id=task["id"],
                    task_kind=task["kind"],
                    worker=worker_identity,
                    claim_id=claim_id,
                )
            return self._task_record(task, batch, claim_id, token, now + int(self.setting("claim_ttl_seconds")))

    def _choose_task(
        self, claimable: list[tuple[dict[str, Any], dict[str, Any]]], concurrency: int
    ) -> tuple[dict[str, Any], dict[str, Any]] | None:
        """The next task on a batch without a live worker claim: the head's task, else an open batch's
        (background) while total claims number fewer than `background_start_limit` and claims on open
        batches number fewer than `concurrency - 1` (one slot is always left for head work, so with
        `repair_concurrency` 1 nothing runs in the background). The role is the batch's, not the
        task kind's: an evicted batch's retained repair is background work, ahead of the other kinds
        by priority. Selection and insertion of the claim share the controller lock; concurrent
        requests cannot select the same batch. When the background slots are full, the next head's
        unclaimed `pre_rebase` takes the slot of the newest own-base preparation of a later batch.
        """
        active = self._active_task_claims()
        held_batches = {claim["batch_id"] for claim in active}
        unheld = [pair for pair in claimable if pair[1]["id"] not in held_batches]
        head = self._head()
        head_id = head["id"] if head is not None else None
        for pair in unheld:
            if pair[1]["id"] == head_id:
                return pair
        background = [claim for claim in active if claim["batch_id"] != head_id]
        if len(active) < int(self.setting("background_start_limit")) and len(background) < concurrency - 1:
            for task, batch in unheld:
                if batch["id"] != head_id:
                    return task, batch
            return None
        # Pre-rebase first: the next head's pre-rebase is on the way to staging, a later batch's
        # own-base preparation is not. When the background slots are full, the newest such
        # preparation gives its slot to the pre-rebase: the claims do not grow, so the capacity kept
        # for head work stays available.
        successor = self._open_batch()
        if successor is None:
            return None
        pre_rebase = next(
            (pair for pair in unheld if pair[1]["id"] == successor["id"] and pair[0]["kind"] == TASK_PRE_REBASE),
            None,
        )
        if pre_rebase is None:
            return None
        preparations = [
            claim
            for claim in background
            if claim["operation"] == f"task:{TASK_PREPARE}"
            and claim["batch_id"] != successor["id"]
            and (self.store.row("tasks", {"id": claim["task_id"]}) or {}).get("pre_rebase_source") is None
        ]
        if not preparations:
            return None
        newest = max(preparations, key=lambda claim: (claim["created_at"], claim["id"]))
        now = self.clock()
        self._supersede_prepare(newest["batch_id"], "pre-rebase-first", now, own_base_only=True)
        self.store.journal(
            now,
            "pre-rebase-first",
            successor["id"],
            task_id=pre_rebase[0]["id"],
            superseded_batch=newest["batch_id"],
            superseded_task=newest["task_id"],
        )
        return pre_rebase

    def _background_claims(self, batch_id: int) -> list[dict[str, Any]]:
        """The live claims on the batch's background tasks (`prepare`, `pre_rebase`)."""
        return [c for c in self._active_claims(batch_id) if c["operation"] in BACKGROUND_OPERATIONS]

    def _claimable_tasks(self) -> list[tuple[dict[str, Any], dict[str, Any]]]:
        """Open tasks whose batch is still at the task's version and in the state the kind works on
        (`open` for `prepare`, `head` otherwise), with their batch, by kind priority then age. A
        `pre_rebase` task instead needs its batch open with the task's chain as a prefix and the head's
        tip unchanged (`_pre_rebase_task_current`): insertion may append to the batch meanwhile; a
        `prepare` task of the pre-rebased chain likewise (`_pre_rebased_prepare_current`)."""
        found = []
        for task in self.store.rows("tasks", {"repository": self.repository, "state": TASK_OPEN}):
            try:
                self._task_binding(task)
            except TaskError:
                continue  # No implicit adoption of old/unqualified policy snapshots.
            batch = self.store.row("batches", {"id": task["batch_id"]})
            if batch is None:
                continue
            if task["kind"] == TASK_PRE_REBASE:
                if self._pre_rebase_task_current(task, batch):
                    found.append((task, batch))
                continue
            if self._pre_rebased_prepare(task):
                if self._pre_rebased_prepare_current(task, batch):
                    found.append((task, batch))
                continue
            if self._stale_prepare(task):
                continue  # superseded on the next pass; never started on a base `main` has moved past
            if task["kind"] != TASK_PREPARE and self._evicted_task_current(task, batch):
                found.append((task, batch))
                continue
            if self._open_rebase_current(task, batch):
                found.append((task, batch))
                continue
            if batch["version"] != task["batch_version"]:
                continue
            wanted = BATCH_OPEN if task["kind"] == TASK_PREPARE else BATCH_HEAD
            if batch["state"] != wanted:
                continue
            found.append((task, batch))
        found.sort(
            key=lambda pair: (
                TASK_PRIORITY.get(pair[0]["kind"], 0),
                not pair[1]["sealed"],
                pair[0]["created_at"],
                pair[0]["id"],
            )
        )
        return found

    def _active_task_claims(self) -> list[dict[str, Any]]:
        """Every worker's live task claim, on any batch: what `repair_concurrency` bounds."""
        return [
            row
            for row in self._live_claims(self.store.rows("claims", {"released_at": None}))
            if row["task_id"] is not None
        ]

    def renew_task(self, task_id: int, claim_token: str, *, source_access=None, source_refresh=None) -> float:
        """Extend the claim by `claim_ttl_seconds` from now.

        The claim's expiry is judged at the request's arrival, not when it gets the lock: a renewal that
        arrived before the claim expired extends it even if a busy tick kept it waiting past the expiry
        (and `_expire_claims` spares the claim while such a renewal waits).
        """
        arrived = self.clock()
        entry_actor = None
        if self.candidate_ci is not None:
            _, entry_actor = self._source_request_access(source_access)
        waiter = object()
        with self._renewing_guard:
            waiting = self._renewal_waiters.setdefault(claim_token, {})
            waiting[waiter] = arrived
            self._renewing[claim_token] = min(waiting.values())
        try:
            with self._lock.priority():
                claim = self._claim_for(task_id, claim_token, arrived)
                if self.candidate_ci is not None:
                    self._check_source_claim_owner(entry_actor, claim)
                    try:
                        self._source_request_access(source_access, claim=claim)
                    except SourceAuthorityUnavailable:
                        source_access = self._refresh_source_access(source_access, source_refresh, claim=claim)
                        claim = self._claim_for(task_id, claim_token, arrived)
                        self._source_request_access(source_access, claim=claim)
                now = self.clock()
                expires = now + int(self.setting("claim_ttl_seconds"))
                self.store.update("claims", {"id": claim["id"]}, expires_at=expires, renewed_at=now)
                return expires
        finally:
            with self._renewing_guard:
                waiting = self._renewal_waiters[claim_token]
                del waiting[waiter]
                if waiting:
                    self._renewing[claim_token] = min(waiting.values())
                else:
                    del self._renewal_waiters[claim_token]
                    self._renewing.pop(claim_token, None)

    def release_task(self, task_id: int, claim_token: str, reason: str, error: str = "", *, source_access=None) -> None:
        """A worker gives a task back. Reason `inference_unavailable` counts toward the inference hold.

        A `prepare` task released as `prepared_clean` (its batch built without an error, so there was
        nothing to repair) records the batch version as prepared; one released after the agent or the
        push failed is resolved as `failed` and not offered again at that version. Any other release
        leaves the task open for the next claim, as for the head's tasks.
        """
        with self._lock.priority():
            now = self.clock()
            claim = self._claim_for(task_id, claim_token, now, allow_stale_policy=True)
            if self.candidate_ci is not None:
                self._source_request_access(source_access, claim=claim)
            task = self.store.row("tasks", {"id": task_id})
            stale_policy = task is not None and not self._task_policy_current(task)
            with self.store.transaction():
                self.store.update("claims", {"id": claim["id"]}, released_at=now, release_reason=reason)
                self.store.journal(now, "released", claim["batch_id"], task_id=task_id, reason=reason, error=error)
                if stale_policy and task["state"] == TASK_OPEN:
                    self._replace_stale_source_task(task, now)
                elif task is not None and task["kind"] == TASK_PREPARE and task["state"] == TASK_OPEN:
                    self._settle_prepare_release(task, reason, now)
                    if reason == PREPARE_CLEAN_REASON:
                        settled = self.store.row("tasks", {"id": task_id}) or {}
                        knowledge = next(
                            (d for d in loads(task["diagnostics"], []) if d.get("kind") == "admission_predictions"), {}
                        )
                        if settled.get("state") == TASK_SUBMITTED and self._predicted_collisions(knowledge):
                            self.store.journal(
                                now,
                                "admission-repair-result",
                                task["batch_id"],
                                task_id=task_id,
                                task_kind=TASK_PREPARE,
                                input_tip=task["tip"],
                                tip=task["tip"],
                                attempt_id=None,
                                edited=[],
                                collisions=self._predicted_collisions(knowledge),
                                verified_fixed=[],
                                verification="prepared clean without source edits",
                            )
            if reason == "inference_unavailable":
                self.report_inference(claim["worker"], False, error)
            self._wake.set()

    def _settle_prepare_release(self, task: dict[str, Any], reason: str, now: float) -> None:
        batch = self.store.row("batches", {"id": task["batch_id"]})
        if self._pre_rebased_prepare(task):
            self._settle_pre_rebased_release(task, batch, reason, now)
            return
        current = batch is not None and batch["state"] == BATCH_OPEN and batch["version"] == task["batch_version"]
        if reason == PREPARE_CLEAN_REASON:
            self.store.update("tasks", {"id": task["id"]}, state=TASK_SUBMITTED, resolved_at=now, resolution="prepared")
            if current:
                assert batch is not None
                self.store.update("batches", {"id": batch["id"]}, prepared_version=batch["version"])
            self._count("prepares_clean")
            self.store.journal(
                now, "prepared", task["batch_id"], task_id=task["id"], version=task["batch_version"], clean=True
            )
        elif reason in PREPARE_FAILED_REASONS:
            self.store.update("tasks", {"id": task["id"]}, state=TASK_SUPERSEDED, resolved_at=now, resolution="failed")
            self._count("prepares_failed")
            self.store.journal(now, "prepare-failed", task["batch_id"], task_id=task["id"], reason=reason)

    def submit_task(
        self,
        task_id: int,
        claim_token: str,
        tip_sha: str,
        worker_notes: str = "",
        *,
        checkpoint: bool = False,
        source_access=None,
        source_refresh=None,
    ) -> Adoption:
        """Validate a proposed chain against the task and adopt it as the batch's next version.

        A late result (expired claim, superseded task or batch version) is rejected but its commits
        stay retained under a submissions ref. See `Adoption` for the reason codes.
        A repair checkpoint takes the same validation path, retains the chain for the next claim
        and releases the current claim without adopting the chain.
        """
        with self._lock.priority():
            now = self.clock()
            task = self.store.row("tasks", {"id": task_id})
            if task is None:
                raise TaskError("unknown-task", str(task_id))
            claim = self.store.row("claims", {"task_id": task_id, "token": claim_token})
            if claim is None:
                raise TaskError("unknown-claim", "no claim with that token on this task")
            if self.candidate_ci is not None:
                self._source_request_access(source_access, claim=claim)
            # A lost HTTP response must not cause a second adoption. This is a
            # receipt for this exact claim/tip, never permission to adopt stale work.
            receipt = self.store.row(
                "submissions",
                {"task_id": task_id, "claim_id": claim["id"], "tip": tip_sha, "accepted": 1},
                order="id DESC",
            )
            if receipt is not None:
                return Adoption(
                    True, "already-adopted", task_id, task["batch_id"], None, tip_sha, receipt["retained_ref"], []
                )
            if checkpoint:
                saved = self.store.row(
                    "submissions",
                    {"task_id": task_id, "claim_id": claim["id"], "tip": tip_sha, "reason": REPAIR_CHECKPOINT},
                )
                if saved is not None:
                    return Adoption(
                        False, REPAIR_CHECKPOINT, task_id, task["batch_id"], None, tip_sha, saved["retained_ref"], []
                    )
            self._task_binding(task, allow_stale_policy=True)
            batch = self.store.row("batches", {"id": task["batch_id"]})
            assert batch is not None
            retained = self._retain_submission(task, claim, tip_sha)
            if self.candidate_ci is not None:
                now = self.clock()

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
                kind = "repair-checkpoint-retained" if reason == REPAIR_CHECKPOINT else "submission-rejected"
                self.store.journal(now, kind, batch["id"], task_id=task_id, reason=reason, tip=tip_sha)
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

            if not self._task_policy_current(task):
                self._replace_stale_source_task(task, now)
                return reject("policy-changed")
            if claim["released_at"] is not None:
                return reject("claim-expired" if claim["release_reason"] == "expired" else "claim-superseded")
            if claim["expires_at"] <= now and not self._renewal_protects(claim):
                self.store.update("claims", {"id": claim["id"]}, released_at=now, release_reason="expired")
                return reject("claim-expired")
            if self.candidate_ci is None and not self._repository_admin(claim["github_login"]):
                return reject("not-authorized")
            if not self._submission_task_current(task, batch):
                return reject("version-mismatch")
            if retained is None:
                return reject("tip-unavailable")
            members = self._task_members(task)
            records = [self._chain_member(member) for member in members]
            report = chain.validate(self.clone, task["onto"], tip_sha, records)
            if self.candidate_ci is not None:
                now = self.clock()
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
            if self.candidate_ci is not None:
                now = self.clock()
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
            edited_no_repair = self._no_repair_edits(task, members, comparison)
            if edited_no_repair:
                violations = [
                    chain.Violation(
                        "no-repair-edited",
                        f"{m.source_pr} is never edited (it opted out of repair or changes control files): leave its "
                        "content alone, or reduce it to an empty commit "
                        "naming the interacting PR, file and finding",
                        commit=m.new_sha,
                        source_pr=m.source_pr,
                    )
                    for m in edited_no_repair
                ]
                return reject("no-repair-edited", violations)
            knowledge = next(
                (d for d in loads(task["diagnostics"], []) if d.get("kind") == "admission_predictions"), {}
            )
            if self.candidate_ci is not None:
                original_task = task
                source_access = self._refresh_source_access(source_access, source_refresh, claim=claim)
                now = self.clock()
                current_task = self.store.row("tasks", {"id": task_id})
                current_claim = self.store.row("claims", {"id": claim["id"]})
                current_batch = self.store.row("batches", {"id": task["batch_id"]})
                if current_task is None or current_batch is None:
                    return reject("version-mismatch")
                if (
                    current_claim is None
                    or current_claim["token"] != claim_token
                    or current_claim["task_id"] != task_id
                ):
                    return reject("claim-superseded")
                task, claim, batch = current_task, current_claim, current_batch
                self._source_request_access(source_access, claim=claim)
                if not self._task_policy_current(task):
                    self._replace_stale_source_task(task, now)
                    return reject("policy-changed")
                if claim["released_at"] is not None:
                    return reject("claim-expired" if claim["release_reason"] == "expired" else "claim-superseded")
                if claim["expires_at"] <= now and not self._renewal_protects(claim):
                    self.store.update("claims", {"id": claim["id"]}, released_at=now, release_reason="expired")
                    return reject("claim-expired")
                identity_fields = (
                    "batch_id",
                    "batch_version",
                    "kind",
                    "base",
                    "tip",
                    "onto",
                    "pre_rebase_source",
                    "ci_attempt_id",
                    "task_binding",
                )
                if any(task[key] != original_task[key] for key in identity_fields):
                    return reject("version-mismatch")
                if not self._submission_task_current(task, batch):
                    return reject("version-mismatch")
                covered = {member["id"] for member in members}
                current_members = [member for member in self._members(batch["id"]) if member["id"] in covered]
                if current_members != members:
                    return reject("version-mismatch")
                self._source_request_access(source_access, claim=claim)
                knowledge = next(
                    (d for d in loads(task["diagnostics"], []) if d.get("kind") == "admission_predictions"), {}
                )
            if checkpoint:
                try:
                    notes = loads(worker_notes, {})
                except ValueError:
                    notes = {}
                findings = notes.get("repair_checkpoint") if isinstance(notes, dict) else None
                if not (
                    self._can_checkpoint_repair(task)
                    and isinstance(findings, list)
                    and 0 < len(findings) <= 20
                    and all(
                        isinstance(finding, dict)
                        and finding.get("kind") in ("audit", "lint")
                        and isinstance(finding.get("message"), str)
                        and len(finding["message"]) <= 2000
                        for finding in findings
                    )
                ):
                    return reject("invalid-repair-checkpoint")
                with self.store.transaction():
                    result = reject(REPAIR_CHECKPOINT)
                    self._release_claim(claim, REPAIR_CHECKPOINT, now)
                self._wake.set()
                return result
            if tip_sha == task["tip"] and knowledge.get("was_proactive"):
                self._release_claim(claim, "source-failure-refresh", now)
                return reject(
                    "claim-superseded",
                    [
                        chain.Violation(
                            "unchanged-source-failure",
                            "The candidate's own CI reported source failures; an unchanged prediction cannot resolve them.",
                        )
                    ],
                )
            if self._predicted_collisions(knowledge):
                self._record_prediction_result(task, batch, comparison, tip_sha, worker_notes, now)
            if task["kind"] == TASK_PRE_REBASE:
                return self._adopt_pre_rebase(task, claim, batch, tip_sha, comparison, worker_notes, now)
            if self._pre_rebased_prepare(task):
                return self._adopt_prepared_pre_rebase(task, claim, batch, tip_sha, comparison, worker_notes, now)
            if self._prediction_task(task) and tip_sha == task["tip"]:
                return self._adopt_prediction_noop(task, claim, batch, tip_sha, retained, worker_notes, now)
            return self._adopt(task, claim, batch, tip_sha, comparison, report, worker_notes, retained, now)

    def _commit_empty(self, sha: str | None) -> bool:
        """A single-parent commit whose tree is its parent's."""
        if not sha:
            return False
        commit = chain.read_commit(self.clone, sha)
        return len(commit.parents) == 1 and commit.tree == chain.tree_of(self.clone, commit.parents[0])

    def _landed_between(self, base: str, onto: str) -> dict[str, str]:
        """The PRs a task's new base carries that its old base did not (`chain.landed_prs`): a member
        emptied because it is already on `main` is not an opt-out withdrawal."""
        if base == onto:
            return {}
        try:
            return chain.landed_prs(self.clone, base, onto)
        except chain.ChainError:
            return {}

    def _no_repair_edits(
        self, task: dict[str, Any], members: list[dict[str, Any]], comparison: chain.ComparisonReport
    ) -> list[chain.MemberProvenance]:
        """Edited members that opted out of repair (or were withdrawn) and are not empty commits: an
        agent may only reduce such a member to an empty commit, never change its content otherwise."""
        guarded = {m["source_pr"] for m in members if m["no_repair"] or m["withdrawn"]}
        if not guarded:
            return []
        landed = self._landed_between(task["base"], task["onto"])
        return [
            m
            for m in comparison.members
            if m.source_pr in guarded
            and m.provenance == chain.EDITED
            and not self._commit_empty(m.new_sha)
            and not chain.landed_commit(landed, m.source_pr)
        ]

    def _worker_submission_ref(self, task_id, claim_id):
        if self.candidate_ci is not None and getattr(self.source, "task_git_transport", None) == NATIVE_GIT_REFS:
            return native_submission_ref(self.candidate_ci.ledger.epoch, task_id, claim_id)
        return self.submission_ref(task_id, claim_id)

    def _retain_submission(self, task: dict[str, Any], claim: dict[str, Any], tip_sha: str) -> str | None:
        ref = self.submission_ref(task["id"], claim["id"])
        remote = self._worker_submission_ref(task["id"], claim["id"])
        if not chain.object_exists(self.clone, tip_sha):
            try:
                self.source.fetch_refs(self.repository, [f"+{remote}:{ref}"])
            except Exception as error:  # noqa: BLE001
                self.log.warning("fetch of %s failed: %s", ref, error)
            if not chain.object_exists(self.clone, tip_sha):
                try:
                    self.source.fetch_refs(self.repository, [tip_sha])
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
        shrunk = False
        landed = self._landed_between(task["base"], task["onto"])
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
                        if not chain.landed_commit(landed, member["source_pr"]):
                            self.store.journal(
                                now,
                                "member-reduced-to-empty",
                                batch["id"],
                                member_id=member["id"],
                                task_id=task["id"],
                                commit=new_sha,
                            )
                        if (
                            member["no_repair"]
                            and not member["withdrawn"]
                            and not chain.landed_commit(landed, member["source_pr"])
                        ):
                            self.store.update("members", {"id": member["id"]}, **values)
                            self._anticipate(batch, dict(member, **values), outcome.explanation.strip(), now)
                            provenance[member["source_pr"]] = "withdrawn"
                            continue
                        self._count("shrink_to_empty")
                        shrunk = True
                        if (
                            self.setting("withdraw_emptied_at_adoption")
                            and not member["withdrawn"]
                            and not chain.landed_commit(landed, member["source_pr"])
                        ):
                            # The agent left this contribution out: send it back to its author now
                            # rather than discovering the empty commit when the batch lands.
                            emptied = dict(member, **values)
                            self.store.update("members", {"id": member["id"]}, **values)
                            self._withdraw_member(
                                batch,
                                emptied,
                                reason=EMPTY_COMMIT,
                                comment=self._emptied_comment(batch, emptied, task, outcome.explanation.strip()),
                                journal_kind="member-withdrawn-empty",
                                now=now,
                                task_id=task["id"],
                            )
                            self._count("prs_withdrawn_empty")
                            provenance[member["source_pr"]] = "withdrawn"
                            continue
                self.store.update("members", {"id": member["id"]}, **values)
                provenance[member["source_pr"]] = values.get("provenance", member["provenance"])
            # An adopted preparation keeps its batch open: the repaired members are what it seals with.
            # So does a retained task of a batch the emergency batch took the head from (`_evict`):
            # its chain was validated as the head, and own-base preparation is not repeated.
            # An open batch's own rebase onto `main` is not a preparation: one on `main` follows it.
            prepared = (
                {"prepared_version": version}
                if task["kind"] == TASK_PREPARE or self._eviction(batch) is not None
                else {}
            )
            self.store.update(
                "batches",
                {"id": batch["id"]},
                version=version,
                base=task["onto"],
                tip=tip_sha,
                **prepared,
            )
            self._record_tip(batch["id"], version, tip_sha, now)
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
            self._count(
                {TASK_REPAIR: "repair_rounds", TASK_PREPARE: "prepares_adopted"}.get(
                    task["kind"], "rebase_conflicts_resolved"
                )
            )
            if shrunk:
                self._discard_pre_rebase("head-shrunk", now, batch)
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

    def _adopt_pre_rebase(
        self,
        task: dict[str, Any],
        claim: dict[str, Any],
        batch: dict[str, Any],
        tip_sha: str,
        comparison: chain.ComparisonReport,
        worker_notes: str,
        now: float,
    ) -> Adoption:
        """Record a resolved pre-rebase on the open batch, which stays open on its own base.

        The batch's members, base, tip and version are unchanged (insertion keeps appending to its own
        chain); the submitted chain becomes the batch's pre-rebase on the head's tip, with the members
        the agent edited and their explanations kept for the seal that adopts it (`_seal_from_pre_rebase`).
        Members inserted after the task was created are replayed onto it by the next `_pre_rebase`.
        """
        edits = {
            m.source_pr: {"explanation": m.explanation.strip(), "task_id": task["id"]}
            for m in comparison.members
            if m.provenance == chain.EDITED
        }
        provenance = {
            m["source_pr"]: "edited" if m["source_pr"] in edits else m["provenance"] for m in self._task_members(task)
        }
        ref = self.pre_rebase_ref(batch["id"], task["onto"])
        prepared = self._verified_pre_rebase(task, tip_sha, worker_notes)
        with self.store.transaction():
            self._set_pre_rebase(
                batch,
                PRE_REBASE_ADOPTED,
                now,
                tip=tip_sha,
                onto=task["onto"],
                source=task["tip"],
                edits=edits,
                prepared=prepared,
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
                if other["id"] != claim["id"] and other["task_id"] is not None:
                    self.store.update("claims", {"id": other["id"]}, released_at=now, release_reason="superseded")
            self.store.insert(
                "submissions",
                task_id=task["id"],
                claim_id=claim["id"],
                tip=tip_sha,
                retained_ref=ref,
                accepted=1,
                reason="adopted",
                worker_notes=worker_notes,
                created_at=now,
            )
            self._count("pre_rebase_conflicts_resolved")
            self.store.journal(
                now,
                "pre-rebase-adopted",
                batch["id"],
                task_id=task["id"],
                onto=task["onto"],
                tip=tip_sha,
                provenance=provenance,
                prepared=prepared,
            )
        self.report_inference(claim["worker"], True)
        self._wake.set()
        return Adoption(True, "adopted", task["id"], batch["id"], batch["version"], tip_sha, ref, provenance=provenance)

    def _verified_pre_rebase(self, task: dict[str, Any], tip: str, worker_notes: str) -> bool:
        from . import repair_audit

        try:
            notes = loads(worker_notes, {})
            evidence = notes.get("pre_rebase_verification") if isinstance(notes, dict) else None
            if not isinstance(evidence, dict) or evidence.get("tip") != tip or evidence.get("base") != task["onto"]:
                return False
            # The commit binds the toolchain, manifest and full-library inputs.
            # Compare the actual tools the worker ran to the pin in that commit.
            repo, ref = repair_audit.parse_tools_pin(chain.git(self.clone, "show", f"{tip}:tauceti.toml"))
        except (ValueError, chain.ChainError, repair_audit.Unavailable):
            return False
        return evidence.get("tools") == f"{repo}@{ref}"

    def _claim_for(self, task_id: int, token: str, now: float, *, allow_stale_policy=False) -> dict[str, Any]:
        claim = self.store.row("claims", {"task_id": task_id, "token": token})
        if claim is None:
            raise TaskError("unknown-claim", "no claim with that token on this task")
        task = self.store.row("tasks", {"id": task_id})
        if task is None:
            raise TaskError("unknown-task")
        self._task_binding(task, allow_stale_policy=allow_stale_policy)
        if claim["released_at"] is not None:
            raise TaskError("claim-expired" if claim["release_reason"] == "expired" else "claim-superseded")
        if claim["expires_at"] <= now and not self._renewal_protects(claim):
            self.store.update("claims", {"id": claim["id"]}, released_at=now, release_reason="expired")
            raise TaskError("claim-expired")
        return claim

    def _renewal_protects(self, claim):
        """An authenticated request delivered before expiry keeps this lease live."""
        with self._renewing_guard:
            arrived = self._renewing.get(claim["token"])
        return arrived is not None and arrived < claim["expires_at"]

    def _open_task(self, head: dict[str, Any]) -> dict[str, Any] | None:
        return self.store.row(
            "tasks", {"batch_id": head["id"], "batch_version": head["version"], "state": TASK_OPEN}, order="id DESC"
        )

    def _task_members(self, task: dict[str, Any]) -> list[dict[str, Any]]:
        """The members a task's chain covers: the batch's, or for a `pre_rebase` task and a `prepare`
        task of the pre-rebased chain those in its `base..tip` (members inserted since are replayed
        onto its result afterwards)."""
        members = self._members(task["batch_id"])
        if task["kind"] != TASK_PRE_REBASE and not self._pre_rebased_prepare(task):
            return members
        covered = {commit.source_pr for commit in chain.chain_commits(self.clone, task["base"], task["tip"])}
        return [member for member in members if member["source_pr"] in covered]

    def _task_binding(self, task, *, allow_stale_policy=False):
        try:
            row = loads(task.get("task_binding"), {})
        except (TypeError, ValueError):
            raise TaskError("not-authorized", "qualified task binding is unreadable") from None
        from .mixed_tasks import SCHEMA as MIXED_TASK_SCHEMA
        from .mixed_tasks import binding as mixed_binding

        if isinstance(row, dict) and row.get("schema") == MIXED_TASK_SCHEMA:
            if self.mixed is None or self.mixed.mode != "admit" or self.mixed_ci is None:
                raise TaskError("not-authorized", "mixed task requires active enrolled sources")
            current = mixed_binding(self.mixed)
            same_scope = all(row.get(key) == current[key] for key in ("github", "forgejo", "epoch"))
            if row != current and not (allow_stale_policy and same_scope):
                raise TaskError("not-authorized", "mixed task policy changed")
            return None  # Worker control and retained refs keep the existing GitHub authority.
        if any(isinstance(member["number"], SourceNumber) for member in self._members(task["batch_id"])):
            # A previously claimed GitHub task keeps its original immutable tip,
            # even if a native member was appended to its batch meanwhile.
            cache = getattr(self, "_task_source_cache", {})
            key = (task["base"], task["tip"])
            if key not in cache:
                cache[key] = any(
                    commit.source_pr and commit.source_pr.startswith("forgejo:") and "@" in commit.source_pr
                    for commit in chain.chain_commits(self.clone, *key)
                )
                if len(cache) > 512:
                    cache.pop(next(iter(cache)))
                self._task_source_cache = cache
            if cache[key]:
                raise TaskError("not-authorized", "mixed task has no frozen source binding")
        if self.candidate_ci is None:
            if not isinstance(row, dict) or row != {}:
                raise TaskError("not-authorized", "native task requires its qualified controller")
            return None
        try:
            binding = TaskBinding.parse(row)
        except (TypeError, ValueError):
            raise TaskError("not-authorized", "qualified task binding is unavailable") from None
        current = TaskBinding.from_policy(self.candidate_ci.ledger.policy, queue_epoch=self.candidate_ci.ledger.epoch)
        same_scope = (binding.source, binding.execution, binding.queue_epoch) == (
            current.source,
            current.execution,
            current.queue_epoch,
        )
        if binding != current and not (allow_stale_policy and same_scope):
            raise TaskError("not-authorized", "qualified task policy changed")
        return binding

    def _task_policy_current(self, task):
        try:
            self._task_binding(task)
            return True
        except TaskError:
            return False

    def _replace_stale_source_task(self, task, now):
        """Replace work under a changed policy; never reinterpret its claim."""
        if task["state"] != TASK_OPEN:
            return None
        batch = self.store.row("batches", {"id": task["batch_id"]})
        replace = batch is not None and self._submission_task_current(task, batch)
        with self.store.transaction():
            self.store.update(
                "tasks", {"id": task["id"]}, state=TASK_SUPERSEDED, resolved_at=now, resolution="policy-changed"
            )
            self.store.update(
                "claims", {"task_id": task["id"], "released_at": None}, released_at=now, release_reason="policy-changed"
            )
            replacement = None
            if replace:
                replacement = self._create_task(
                    batch,
                    task["kind"],
                    task["onto"],
                    loads(task["diagnostics"], []),
                    now,
                    ci_attempt_id=task["ci_attempt_id"],
                    pre_rebased=(task["tip"], task["pre_rebase_source"]) if self._pre_rebased_prepare(task) else None,
                )
                marker = self._eviction(batch)
                if marker is not None and task["id"] in marker["task_ids"]:
                    # The replacement of an evicted batch's retained task is retained too.
                    marker = dict(marker, task_ids=[*marker["task_ids"], replacement])
                    self._set_state(f"emergency_evicted:{batch['id']}", marker)
            self.store.journal(
                now, "task-policy-changed", task["batch_id"], task_id=task["id"], replacement_task_id=replacement
            )
        self._wake.set()
        return replacement

    def _supersede_stale_source_tasks(self, now):
        if self.candidate_ci is None and self.mixed_ci is None:
            return
        for previous in self.store.rows("tasks", {"repository": self.repository, "state": TASK_OPEN}):
            task = self.store.row("tasks", {"id": previous["id"]})
            if task is not None and task["state"] == TASK_OPEN and not self._task_policy_current(task):
                self._replace_stale_source_task(task, now)

    def _task_record(
        self, task: dict[str, Any], batch: dict[str, Any], claim_id: int, token: str, expires: float
    ) -> Task:
        members = []
        for member in self._task_members(task):
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
                    no_repair=bool(member["no_repair"] or member["withdrawn"]),
                    withdrawn=bool(member["withdrawn"]),
                    declaration_names=loads(input_row.get("declaration_names"), {}),
                    large_refactor=self._large_refactor_member(member, input_row),
                )
            )
        refs = [self.batch_ref(batch["id"], task["batch_version"])] + [m.retained_ref for m in members]
        extra: dict[str, Any] = {
            "canonical_branch": self.canonical_branch,
            "claim_id": claim_id,
        }
        if loads(task.get("task_binding"), {}).get("schema") == "tauceti-queue.mixed-task/v1":
            if self.mixed is None:
                raise TaskError("source-disabled", "mixed task requires its enrolled sources")
            self._task_binding(task)
            extra["mixed_binding"] = loads(task["task_binding"])
        warm_revs = self._warm_revs(task, batch)
        if self._can_checkpoint_repair(task):
            extra["can_checkpoint_repair"] = True
            checkpoint = self.store.row(
                "submissions", {"task_id": task["id"], "reason": REPAIR_CHECKPOINT}, order="id DESC"
            )
            if checkpoint is not None:
                tip = checkpoint["tip"]
                extra["repair_checkpoint"] = {
                    "tip": tip,
                    "diagnostics": loads(checkpoint["worker_notes"])["repair_checkpoint"],
                }
                refs.append(checkpoint["retained_ref"])
                warm_revs = list(dict.fromkeys([tip, *warm_revs]))
        if task["kind"] in BACKGROUND_TASKS or batch["state"] == BATCH_OPEN:
            extra["renew_after"] = HANDOFF_RENEW_SECONDS
            extra["background"] = True  # work for a later batch: its builds yield the CPU to the head's
        if self._pre_rebased_prepare(task):
            # The pre-rebased chain exists only in the controller's clone until `_prepare_pre_rebased`
            # retains it on GitHub; the worker builds on the head's tip and falls back to the public
            # cache at the head's base, then the batch's own base, when no repair outputs are published.
            refs.insert(1, self.pre_rebased_ref(batch["id"], task["tip"]))
            ahead = self._head()
            extra["pre_rebased"] = {
                "source": task["pre_rebase_source"],
                "batch_base": batch["base"],
                "head_batch_id": ahead["id"] if ahead is not None else None,
                "head_base": ahead["base"] if ahead is not None and ahead["tip"] == task["onto"] else None,
            }
            if ahead is not None and ahead["tip"] == task["onto"]:
                refs.insert(2, self.batch_ref(ahead["id"], ahead["version"]))
        if task["kind"] == TASK_PRE_REBASE:
            ahead = self._head()
            if ahead is not None and ahead["tip"] == task["onto"]:
                refs.insert(1, self.batch_ref(ahead["id"], ahead["version"]))  # where `onto` is fetched from
        if self.candidate_ci is not None and getattr(self.source, "task_git_transport", None) == NATIVE_GIT_REFS:
            refs = [native_retained_ref(ref) for ref in refs]
            members = [
                dataclasses.replace(member, retained_ref=native_retained_ref(member.retained_ref)) for member in members
            ]
            extra["git_transport"] = NATIVE_GIT_REFS
        return Task(
            task_id=task["id"],
            kind=task["kind"],
            repository="" if self.candidate_ci is not None else self.repository,
            batch_id=batch["id"],
            batch_version=task["batch_version"],
            base=task["base"],
            tip=task["tip"],
            onto=task["onto"],
            members=members,
            diagnostics=loads(task["diagnostics"], []),
            retained_refs=refs,
            submission_ref=self._worker_submission_ref(task["id"], claim_id),
            claim_token=token,
            expires_at=expires,
            instruction=AGENT_INSTRUCTION,
            created_at=task["created_at"],
            extra=extra,
            warm_revs=warm_revs,
            binding=self._task_binding(task),
        )

    def _can_checkpoint_repair(self, task: dict[str, Any]) -> bool:
        return self.candidate_ci is None and task["kind"] in (TASK_REPAIR, TASK_REBASE_CONFLICT, TASK_PRE_REBASE)

    def _create_task(
        self,
        head: dict[str, Any],
        kind: str,
        onto: str,
        diagnostics: list[dict[str, Any]],
        now: float,
        ci_attempt_id: int | None = None,
        *,
        pre_rebased: tuple[str, str] | None = None,
    ) -> int:
        """Create the batch's next task of `kind` on `onto`, superseding what it replaces. A `prepare`
        task of the pre-rebased chain passes `pre_rebased` (its tip, and the batch tip it covers): its
        chain is `onto..tip` instead of the batch's own `base..tip`."""
        knowledge = self._admission_context(head, now)
        if knowledge.get("records") and not any(d.get("kind") == "admission_predictions" for d in diagnostics):
            diagnostics = [*diagnostics, knowledge]
        diagnostics = [
            admission_planning.task_context(d) if d.get("kind") == "admission_predictions" else d for d in diagnostics
        ]
        collisions = self._predicted_collisions(knowledge)
        if kind == TASK_PREPARE and collisions:
            self._set_state(
                f"admission_prepare:{head['id']}", admission_planning.fingerprint([{"base": head["base"]}, *collisions])
            )
        # The head's task replaces any other; an open batch's background tasks (`prepare`, `pre_rebase`)
        # each replace only their own kind.
        task_binding = {}
        if self.candidate_ci is not None:
            task_binding = TaskBinding.from_policy(
                self.candidate_ci.ledger.policy, queue_epoch=self.candidate_ci.ledger.epoch
            ).record()
        elif (
            self.mixed is not None
            and self.mixed.mode == "admit"
            and (
                any(isinstance(member["number"], SourceNumber) for member in self._members(head["id"]))
                or (
                    (attempt := self._current_attempt(head)) is not None
                    and self.store.row("ci_requests", {"attempt_id": attempt["id"]}) is not None
                )
            )
        ):
            if self.mixed_ci is None:
                raise TaskError("source-disabled", "mixed task requires active enrolled sources")
            from .mixed_tasks import binding

            task_binding = binding(self.mixed)
        where: dict[str, Any] = {"batch_id": head["id"], "state": TASK_OPEN}
        if kind in (TASK_PREPARE, TASK_PRE_REBASE):
            where["kind"] = kind
        with self.store.transaction():
            for stale in self.store.rows("tasks", where):
                self.store.update(
                    "tasks", {"id": stale["id"]}, state=TASK_SUPERSEDED, resolved_at=now, resolution="replaced"
                )
            task_id = self.store.insert(
                "tasks",
                kind=kind,
                repository=self.repository,
                batch_id=head["id"],
                batch_version=head["version"],
                base=onto if pre_rebased else head["base"],
                tip=pre_rebased[0] if pre_rebased else head["tip"],
                onto=onto,
                diagnostics=dumps(diagnostics),
                state=TASK_OPEN,
                created_at=now,
                ci_attempt_id=ci_attempt_id,
                pre_rebase_source=pre_rebased[1] if pre_rebased else None,
                task_binding=dumps(task_binding),
            )
            self._count("tasks_prepare_pre_rebased" if pre_rebased else f"tasks_{kind}")
            self.store.journal(
                now, "task", head["id"], task_id=task_id, task_kind=kind, onto=onto, pre_rebased=bool(pre_rebased)
            )
        return task_id

    # --- claims ------------------------------------------------------------------------------------

    def _live_claims(self, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """A renewal delivered before expiry keeps ownership while it waits for the controller lock."""
        now = self.clock()
        with self._renewing_guard:
            renewing = dict(self._renewing)
        return [
            row
            for row in rows
            if row["expires_at"] > now or (row["token"] in renewing and renewing[row["token"]] < row["expires_at"])
        ]

    def _active_claims(self, batch_id: int, task_id: int | None = None) -> list[dict[str, Any]]:
        where: dict[str, Any] = {"batch_id": batch_id, "released_at": None}
        if task_id is not None:
            where["task_id"] = task_id
        return self._live_claims(self.store.rows("claims", where))

    def _expire_claims(self, now: float) -> None:
        """Release the claims past their expiry, except one whose renewal arrived in time and is waiting
        for the lock (`renew_task`)."""
        with self._renewing_guard:
            renewing = dict(self._renewing)
        for claim in self.store.rows("claims", {"released_at": None}):
            if claim["token"] in renewing and renewing[claim["token"]] < claim["expires_at"]:
                continue
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

    def _inert_barrier(self):
        if self.mixed is not None and self.mixed.mode == "admit":
            return None
        batches = self.store.rows(
            "batches", {"repository": self.repository, "state": [BATCH_OPEN, BATCH_HEAD]}, order="id"
        )
        return next(
            (batch["id"] for batch in batches if self._mixed_inert_batch(batch) or self._inert_ci_batch(batch)), None
        )

    def _open_batches(self, *, include_inert=False) -> list[dict[str, Any]]:
        """Keep staging order, holding an unavailable source and its successors."""
        batches = self.store.rows("batches", {"repository": self.repository, "state": BATCH_OPEN}, order="id")
        barrier = None if include_inert else self._inert_barrier()
        return [batch for batch in batches if barrier is None or batch["id"] < barrier]

    def _staging_batches(self) -> list[dict[str, Any]]:
        """The open batches ordinary scheduling works on: `_open_batches` without the emergency batch
        and, while the lane is active, the control-file lane batch.

        Insertion, background tasks and sealing order use this; enumeration that must see every live
        batch (retained refs, sources in flight, status) keeps `_open_batches`. With the lane off, a
        live lane batch is staged here as an ordinary open batch, so turning it off needs no cleanup."""
        lane = self._control_lane_active()
        return [
            batch
            for batch in self._open_batches()
            if batch["urgent"] != URGENT_EMERGENCY and not (lane and batch["lane"] == LANE_CONTROL)
        ]

    # --- the control-file lane ---------------------------------------------------------------------

    def _control_lane_active(self) -> bool:
        """Whether the control-file lane applies: on, with mixed sources admitted under GitHub primary
        and no source-candidate CI for GitHub batches. Under Forgejo primary every batch goes through
        source-candidate CI, which cannot authorize control files, so such a PR keeps waiting there."""
        return bool(
            self.setting("control_lane_enabled")
            and self.mixed is not None
            and self.mixed.mode == "admit"
            and self.mixed.primary == "github"
            and self.candidate_ci is None
        )

    @staticmethod
    def _control_input(row: dict[str, Any]) -> bool:
        """The input was classified as a control-file change (`_classify_control_inputs`)."""
        return bool(loads(row.get("evidence"), {}).get(CONTROL_EVIDENCE))

    def _lane_batch(self) -> dict[str, Any] | None:
        """The open control-file lane batch, if any; through `_open_batches`, so an inert barrier hides it
        like any later batch."""
        return next((batch for batch in self._open_batches() if batch["lane"] == LANE_CONTROL), None)

    def _lane_members(self, batch: dict[str, Any]) -> list[dict[str, Any]]:
        """The lane batch's members that act for their input: not withdrawn, and not a slot an emergency
        request took its PR from (`_displaced`)."""
        return [m for m in self._active_members(batch) if not self._displaced(batch, m)]

    def _lane_ready(self) -> dict[str, Any] | None:
        """The open lane batch when `_seal` may make it the head: the lane is active and the batch has a
        member that acts for its input and is not retained for a disabled source."""
        if not self._control_lane_active():
            return None
        batch = self._lane_batch()
        if (
            batch is None
            or not self._lane_members(batch)
            or self._mixed_inert_batch(batch)
            or self._inert_ci_batch(batch)
        ):
            return None
        return batch

    def _ordinary_waiting(self) -> list[dict[str, Any]]:
        """`_waiting` without the inputs the active lane takes, which ordinary insertion never probes: a
        GitHub input enters an ordinary batch only once it is classified as not a control-file change
        (one approved while the tick ran is classified in the next)."""
        rows = [row for row in self._waiting() if not self._label_blocked(row)]
        if not self.setting("forgejo_placement_enabled"):
            rows = [row for row in rows if not isinstance(row["number"], SourceNumber)]
        if not self._control_lane_active():
            return rows
        return [
            row
            for row in rows
            if isinstance(row["number"], SourceNumber) or loads(row["evidence"], {}).get(CONTROL_EVIDENCE) is False
        ]

    def _control_change(self, number: Any, head: str, canonical: str) -> bool | None:
        """Whether a GitHub PR's own diff (its head against its merge base with `canonical`) leaves the
        native path policy, the paths source-candidate CI refuses; None when that cannot be read now.
        Run without the lock: it may fetch the head."""
        if not self._fetch_head(number, head):
            return None
        try:
            base = chain.git(self.clone, "merge-base", canonical, head).strip()
        except chain.ChainError:
            base = canonical  # no merge base: the whole difference from `main` counts
        try:
            return not self.mixed.supports_candidate(base, head)
        except chain.ChainError as error:
            self.log.warning("control-file classification of #%s failed: %s", number, error)
            return None

    def _classify_control_inputs(self, now: float) -> None:
        """Classify each waiting GitHub input once, before routing: a control-file change goes to the lane
        and is never repaired (`inputs.no_repair`, durable, see `_no_repair_event`). An input row is one
        accepted head (a push makes a new row), so the classification never needs to change."""
        canonical = self._canonical
        if canonical is None or not self._control_lane_active():
            return
        for row in self._waiting():
            if isinstance(row["number"], SourceNumber) or CONTROL_EVIDENCE in loads(row["evidence"], {}):
                continue
            with self._lock.unlocked():
                control = self._control_change(row["number"], row["head_sha"], canonical)
            fresh = self.store.row("inputs", {"id": row["id"]})
            if fresh is None or fresh["state"] not in (INPUT_WAITING, INPUT_RESERVED):
                continue
            if control is None:
                if fresh["last_probe"] != CONTROL_UNCLASSIFIED:
                    self.store.update("inputs", {"id": row["id"]}, last_probe=CONTROL_UNCLASSIFIED)
                continue
            evidence = loads(fresh["evidence"], {})
            evidence[CONTROL_EVIDENCE] = control
            with self.store.transaction():
                self.store.update(
                    "inputs", {"id": row["id"]}, evidence=dumps(evidence), **({"no_repair": 1} if control else {})
                )
                if control:
                    self.store.journal(now, "control-classified", number=row["number"], head=row["head_sha"])

    def _lane_probe_eligible(self, row: dict[str, Any], batch: dict[str, Any]) -> bool:
        """`_probe_eligible` for the lane batch. A `source-ci-scope` refusal says nothing about this
        batch's tip (it was about the batch-mates), so its recorded tip does not stop the probe."""
        if row["last_probe"].startswith("source-ci-scope:"):
            row = dict(row, last_probe_tip="")
        return self._probe_eligible(row, batch)

    def _control_lane_insertion(self, now: float, probed: set[tuple[int, int]], started: float, budget: float) -> None:
        """Probe the waiting control-file inputs into the lane batch, first in first out, creating it on
        the canonical commit when none is open. Shares the tick's probe budget and `probed` set."""
        if not self._control_lane_active():
            return
        self._classify_control_inputs(now)
        canonical = self._canonical
        rows = [row for row in self._waiting() if self._control_input(row) and not self._label_blocked(row)]
        if canonical is None or not rows:
            return
        lane = self._lane_batch()
        if lane is not None and not self._members(lane["id"]) and lane["base"] != canonical:
            self._remove_batch(lane, "stale-empty", now)
            lane = None
        if lane is None:
            with self.store.transaction():
                lane = self._create_open_batch(canonical, now)
                self.store.update("batches", {"id": lane["id"]}, lane=LANE_CONTROL)
                self.store.journal(now, "control-lane-opened", lane["id"], base=canonical)
        for row in rows:
            if probed and time.monotonic() - started >= budget:
                self._reasons.append(f"insertion paused after {len(probed)} probes (budget)")
                self._wake.set()
                return
            fresh = self.store.row("inputs", {"id": row["id"]})
            lane = self.store.row("batches", {"id": lane["id"]}) or lane
            if lane["state"] != BATCH_OPEN:
                return
            if (
                fresh is None
                or fresh["state"] != INPUT_WAITING
                or (fresh["id"], lane["id"]) in probed
                or not self._lane_probe_eligible(fresh, lane)
            ):
                continue
            probed.add((fresh["id"], lane["id"]))
            self._probe(fresh, lane, now)

    def _unsealed_batches(self) -> list[dict[str, Any]]:
        """Batches still accepting insertions; only these count against `open_batch_limit`."""
        return [batch for batch in self._staging_batches() if not batch["sealed"]]

    def _open_batch(self) -> dict[str, Any] | None:
        batches = self._staging_batches()
        return batches[0] if batches else None

    def _emergency_batch(self) -> dict[str, Any] | None:
        """The live (open or head) emergency batch; there is at most one."""
        return self.store.row(
            "batches",
            {"repository": self.repository, "state": [BATCH_OPEN, BATCH_HEAD], "urgent": URGENT_EMERGENCY},
            order="id",
        )

    def _head(self) -> dict[str, Any] | None:
        return self.store.row("batches", {"repository": self.repository, "state": BATCH_HEAD}, order="id DESC")

    @staticmethod
    def _large_refactor_member(member: dict[str, Any], row: dict[str, Any]) -> bool:
        """Admission found an admin's `large-refactor` label on this member's accepted head, so
        repair's review-size checks skip the size caps for it, as staging does."""
        if row.get("head_sha") != member["accepted_head"]:
            return False
        return loads(row.get("evidence"), {}).get("large_refactor") is True

    def _members(self, batch_id: int) -> list[dict[str, Any]]:
        return self.store.rows("members", {"batch_id": batch_id, "removed": 0}, order="position")

    def _chain_member(self, member: dict[str, Any]) -> chain.Member:
        input_row = self.store.row("inputs", {"id": member["input_id"]}) or {}
        coauthors = loads(input_row.get("coauthors"))
        return chain.Member(
            self._member_repository(member["number"]),
            int(member["number"]),
            member["accepted_head"],
            title=input_row.get("title", ""),
            description=input_row.get("description", ""),
            author=""
            if self.native_source or isinstance(member["number"], SourceNumber)
            else input_row.get("author", ""),
            coauthors=None if coauthors is None else tuple(coauthors),
        )

    def _mixed_inert_batch(self, batch):
        return bool(
            batch
            and any(isinstance(member["number"], SourceNumber) for member in self._members(batch["id"]))
            and (self.mixed is None or self.mixed.mode != "admit")
        )

    def _inert_ci_batch(self, batch):
        if batch is None or self.mixed_ci is not None or self.candidate_ci is not None:
            return False
        attempt = self._current_attempt(batch)
        return bool(attempt and self.store.row("ci_requests", {"attempt_id": attempt["id"]}))

    def _candidate_coordinator(self, *, batch_id=None, attempt_id=None, run_id=None):
        if self.candidate_ci is not None:
            return self.candidate_ci
        if self.mixed_ci is None:
            owned = False
            if attempt_id is not None:
                owned = self.store.row("ci_requests", {"attempt_id": attempt_id}) is not None
            elif run_id is not None:
                owned = bool(self.store.query("SELECT 1 FROM ci_request_runs WHERE run_id=? LIMIT 1", (run_id,)))
            else:
                batch = self.store.row("batches", {"id": batch_id}) if batch_id is not None else self._head()
                owned = self._inert_ci_batch(batch)
            if owned:
                raise ControllerError(
                    "forgejo-inert", "qualified CI ownership is retained until its coordinator is enabled"
                )
            return None
        if run_id is not None:
            rows = self.store.query(
                "SELECT r.attempt_id FROM ci_requests r JOIN ci_request_runs o ON o.request_digest=r.request_digest WHERE o.run_id=?",
                (run_id,),
            )
            return self.mixed_ci if rows else None
        if attempt_id is not None:
            return self.mixed_ci if self.store.row("ci_requests", {"attempt_id": attempt_id}) is not None else None
        batch = self.store.row("batches", {"id": batch_id}) if batch_id is not None else self._head()
        if batch is None:
            return None
        native = any(isinstance(member["number"], SourceNumber) for member in self._members(batch["id"]))
        if native:
            return self.mixed_ci
        previous = self.store.rows("ci_attempts", {"batch_id": batch["id"]}, order="id DESC", limit=1)
        if previous:
            return self._candidate_coordinator(attempt_id=previous[0]["id"])
        return self.mixed_ci if self.mixed.primary == "forgejo" else None

    def _current_attempt(self, head: dict[str, Any]) -> dict[str, Any] | None:
        rows = self.store.query(
            "SELECT * FROM ci_attempts WHERE batch_id=? AND batch_version=? "
            "AND NOT (dispatch_state='rejected' AND outcome='superseded') ORDER BY id DESC LIMIT 1",
            (head["id"], head["version"]),
        )
        return rows[0] if rows else None

    def _record_tip(self, batch_id: int, version: int, tip: str, now: float) -> None:
        """Record `tip` as the batch's tip at `version` in `batch_tips`; a version already recorded is kept."""
        self.store.execute(
            "INSERT OR IGNORE INTO batch_tips (batch_id, version, tip, recorded_at) VALUES (?, ?, ?, ?)",
            (batch_id, version, tip, now),
        )

    def _repair_outputs_revs(self, head: dict[str, Any]) -> list[str]:
        """The revisions whose published build outputs CI may restore when the head's own are missing.

        The repair worker publishes Lake outputs keyed by the tip it verified, and CI fetches them for
        the exact head, which a rebase replaces. Lake reuses artifacts by content trace, so the outputs
        of a nearly identical tree are safe and nearly complete: the batch's previous tips (newest
        first, at most `REPAIR_OUTPUTS_PREVIOUS_TIPS`), then the tips of the most recently landed heads
        (`_landed_tips`), then its base, then the canonical commit when it differs. No revision appears
        twice and the head's own tip, which CI is given separately, is left out.
        """
        revs = self._previous_tips(head)

        def add(sha: str | None) -> None:
            if sha and sha != head["tip"] and sha not in revs:
                revs.append(sha)

        for tip in self._landed_tips(head["id"]):
            add(tip)
        add(head["base"])
        add(self._canonical)
        return revs

    def _previous_tips(self, batch: dict[str, Any], version: int | None = None) -> list[str]:
        """The batch's tips before `version` (default its current version), newest first, at most
        `REPAIR_OUTPUTS_PREVIOUS_TIPS`, without repeats and without its tip at `version`."""
        version = batch["version"] if version is None else version
        current = self.store.row("batch_tips", {"batch_id": batch["id"], "version": version})
        exclude = {batch["tip"] if version == batch["version"] else None, current["tip"] if current else None}
        tips: list[str] = []
        for row in self.store.rows("batch_tips", {"batch_id": batch["id"]}, order="version DESC"):
            if len(tips) >= REPAIR_OUTPUTS_PREVIOUS_TIPS:
                break
            if row["version"] < version and row["tip"] and row["tip"] not in exclude and row["tip"] not in tips:
                tips.append(row["tip"])
        return tips

    def _landed_tips(self, exclude: int | None = None) -> list[str]:
        """The published tips of the last `REPAIR_OUTPUTS_LANDED_HEADS` landed batches, newest batch
        first and each batch's tips newest first (at most `REPAIR_OUTPUTS_PREVIOUS_TIPS` per batch),
        without repeats and without batch `exclude`.

        A landed head's tip is published to the repair cache by the worker that verified it, but the
        `main` commit it lands as reaches the public cache only once main's own CI has built it, and
        the next head is based on that commit, whose tree differs from the landed tip's only in CI files.
        """
        tips: list[str] = []
        landed = self.store.rows(
            "batches",
            {"repository": self.repository, "state": BATCH_LANDED},
            order="landed_at DESC, id DESC",
            limit=REPAIR_OUTPUTS_LANDED_HEADS + (exclude is not None),
        )
        for batch in [b for b in landed if b["id"] != exclude][:REPAIR_OUTPUTS_LANDED_HEADS]:
            own: list[str] = []
            for row in self.store.rows("batch_tips", {"batch_id": batch["id"]}, order="version DESC"):
                if len(own) >= REPAIR_OUTPUTS_PREVIOUS_TIPS:
                    break
                if row["tip"] and row["tip"] not in own:
                    own.append(row["tip"])
            tips.extend(tip for tip in own if tip not in tips)
        return tips

    def _warm_revs(self, task: dict[str, Any], batch: dict[str, Any]) -> list[str]:
        """The revisions whose published repair outputs the worker restores before the agent runs.

        A `pre_rebase` task builds on the head's tip, which is never in the public cache: the head's
        current tip (the task's `onto` first should the head have moved since), its previous tips
        (newest first, at most `REPAIR_OUTPUTS_PREVIOUS_TIPS`), then the head's base. A `prepare` task
        of the pre-rebased chain builds there too: the head's tips, then the batch's own tips (the one
        the pre-rebase covers, then the earlier ones). A `prepare` task on the batch's own base: the
        batch's previous tips, then that base. A `repair` or `rebase_conflict` task on the head: the
        head's current tip, its previous tips, then its base and the canonical commit. Every kind also
        names the tips of the most recently landed heads (`_landed_tips`) after the batch tips and
        before the base. Lake reuses artifacts by content trace, so a nearly identical tree's outputs
        serve nearly completely.
        """
        revs: list[str] = []

        def add(sha: str | None) -> None:
            if sha and sha not in revs:
                revs.append(sha)

        def head_tips() -> dict[str, Any] | None:
            add(task["onto"])
            ahead = self._head()
            if ahead is None or ahead["id"] == batch["id"]:
                return None
            add(ahead["tip"])
            for tip in self._previous_tips(ahead):
                add(tip)
            return ahead

        def landed() -> None:
            for tip in self._landed_tips(batch["id"]):
                add(tip)

        if task["kind"] == TASK_PRE_REBASE:
            ahead = head_tips()
            landed()
            if ahead is not None:
                add(ahead["base"])
        elif self._pre_rebased_prepare(task):
            # The pre-rebased chain sits on the head's tip: the head's tips, then the batch's own tips
            # (the one the pre-rebase covers first), whose outputs a preparation on its own base published.
            head_tips()
            add(task["pre_rebase_source"])
            for tip in self._previous_tips(batch, task["batch_version"]):
                add(tip)
            landed()
        elif task["kind"] == TASK_PREPARE:
            for tip in self._previous_tips(batch, task["batch_version"]):
                if tip != task["tip"]:
                    add(tip)
            landed()
            add(task["base"])
        elif task["kind"] in (TASK_REPAIR, TASK_REBASE_CONFLICT):
            # The head's tip is never in the public cache, but a worker that verified it, or one of its
            # earlier tips, published its outputs: its current tip, its previous tips, then main.
            add(task["tip"])
            add(batch["tip"])
            for tip in self._previous_tips(batch):
                add(tip)
            landed()
            add(task["base"])
            add(self._canonical)
        return revs

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

    def change_primary(self, target: str, actor: str) -> dict:
        """Change landing authority and sync direction without disturbing admissions or CI."""
        if target not in {"github", "forgejo"}:
            raise ValueError("primary must be github or forgejo")
        with self._ticking, self._lock:
            if self.mixed is None or self.mixed.mode != "admit":
                raise ControllerError("forgejo-inert", "primary changes require active enrolled sources")
            self.mixed.check_writer()
            # An uncertain leased write must be read back in its original direction.
            self._drain_outbox(self.clock())
            from .mixed_landing import tips

            with self._lock.unlocked():
                current = tips(self.mixed)
                self.mixed.check_github()
                check_actor = getattr(self.mixed.native_main, "check_actor", None)
                if check_actor is not None:
                    check_actor()
                if target == "forgejo":
                    if any(
                        not self.mixed.supports_candidate(batch["base"], batch["tip"])
                        for batch in self.store.rows(
                            "batches", {"repository": self.repository, "state": [BATCH_OPEN, BATCH_HEAD]}
                        )
                    ):
                        raise ControllerError(
                            "github-primary-required", "land pending human control-file changes on GitHub first"
                        )
                    self.mixed.mirror.preflight(self.mixed.native, current[target], current[target], current["github"])
            if current["github"] != current["forgejo"]:
                raise ControllerError(
                    "main-tips-differ", "synchronize both protected main tips before changing primary"
                )
            for entry in self.store.rows("outbox", {"kind": "promote"}):
                intent = loads(entry["payload"], {})
                phase = intent.get("promotion_send", {}).get("state")
                unknown = phase == "sending" or phase is None and entry["attempts"] and entry["state"] == "pending"
                if not unknown:
                    continue
                original = intent.get("primary", "github")
                send = intent.get("promotion_send", {})
                expected = intent.get("expected_parent")
                observed = current[original]
                cannot_apply = (
                    expected
                    and observed != expected
                    and (send.get("lease") is True or not chain.is_ancestor(self.clone, observed, intent["tip"]))
                )
                if chain.is_ancestor(self.clone, intent["tip"], observed) or cannot_apply:
                    intent["promotion_send"] = {"state": "settled"}
                    self.store.update("outbox", {"id": entry["id"]}, payload=dumps(intent))
                else:
                    raise ControllerError(
                        "landing-uncertain",
                        "reconcile a sent landing in its original direction before changing primary",
                    )
            if target == self.mixed.primary:
                return {
                    "changed": False,
                    "primary": target,
                    "main": current[target],
                    "generation": self.mixed.generation,
                }
            generation = self.mixed.generation + 1
            with self.store.transaction():
                self.store.set_setting("mixed:primary", target, actor, self.clock())
                self.store.set_setting("mixed:primary_generation", generation, actor, self.clock())
                if target == "github":
                    for row in self._waiting():
                        if row["last_probe"].startswith("source-ci-scope:"):
                            self.store.update(
                                "inputs",
                                {"id": row["id"]},
                                last_probe="",
                                last_probe_tip="",
                                last_probe_base="",
                                probe_conflicts="{}",
                            )
                self._set_state("canonical_sha", current[target])
                self.store.journal(
                    self.clock(),
                    "primary-changed",
                    previous=self.mixed.primary,
                    primary=target,
                    generation=generation,
                    main=current[target],
                    actor=actor,
                )
            self.mixed.primary, self.mixed.generation = target, generation
            self._canonical = current[target]
            self._retain_local(self._canonical_ref(), current[target])
            self._wake.set()
            return {"changed": True, "primary": target, "main": current[target], "generation": generation}

    # --- the reconciliation pass -------------------------------------------------------------------

    def tick(self) -> list[str]:
        """One reconciliation pass; returns the reasons the queue is waiting (also reported in status).

        The pass ends by taking the status snapshot `status()` serves while a later pass holds the lock.
        """
        with self._ticking, self._lock:
            try:
                return self._tick()
            finally:
                # Also after a failed pass: the build's own progress does not depend on GitHub.
                try:
                    self._read_live_progress(self.clock())
                except Exception:  # noqa: BLE001 - observational; never disturbs reconciliation
                    logging.getLogger(__name__).exception("live progress read not scheduled")
                self._refresh_snapshot()

    def _tick(self) -> list[str]:
        """The pass, holding the controller lock except around its GitHub I/O (the outbox drain, the
        canonical fetch, each probe's fetch and replay) and offering it to the waiting worker
        requests between steps (`ControllerLock`), so a worker endpoint waits for one step at most."""
        offer = self._lock.offer
        self._reasons = []
        now = self.clock()
        self._supersede_stale_source_tasks(now)
        self._expire_claims(now)
        # Before anything that can end the pass early: the message depends only on the report.
        self._notify_stuck_heads(now)
        if self.mixed is not None and self.mixed.mode == "admit" and self.setting("publication_enabled"):
            sync_key = f"mixed-sync:startup:{self.mixed.generation}:{self.revision}"
            self._enqueue("mixed-sync", sync_key, {})
        self._drain_outbox(now)
        offer()
        try:
            with self._lock.unlocked():
                canonical = self._fetch_canonical()
        except Exception as error:  # noqa: BLE001 - the pass reports the failure and waits for the next one
            self._reasons.append(f"canonical branch unavailable: {error}")
            return list(self._reasons)
        self._observe_canonical(canonical, now)
        if self._mixed_inert_batch(self._head()) or self._inert_ci_batch(self._head()):
            self._reasons.append("Forgejo admission is disabled; mixed work is retained pending explicit enablement")
            return list(self._reasons)
        self._follow_anticipated(now)
        self._recheck_missing_sources(now)
        hold = self._state("inference_hold")
        if hold:
            self._reasons.append(f"inference hold since {hold['since']:.0f}: {hold['last_error']}")
        offer()
        self._fetch_declaration_names(now)
        self._insertion_passes(now)
        for step in (
            self._supersede_stale_pre_rebase,
            self._emergency,
            self._seal,
            self._reconcile_head,
            self._pre_rebase,
            self._prepare_open_batches,
            self._prepare_pre_rebased,
            self._republish_retained_refs,
        ):
            offer()
            step(now)
        offer()
        self._drain_outbox(self.clock())
        self._set_state("last_tick_at", now)
        return list(self._reasons)

    def _read_live_progress(self, now: float) -> None:
        """Start a background read of the head run's live build report when one is due.

        Runs on the reconciliation thread, which also persists the run's progress watermark (the
        `live-progress` setting) so a restart does not report a stall the previous process had
        seen progress against. The background thread only reads the public report."""
        head = self._head()
        attempt = self._current_attempt(head) if head is not None else None
        if (
            self.live_progress is None
            or attempt is None
            or attempt["outcome"] != OUTCOME_PENDING
            or not attempt["run_id"]
        ):
            return
        key = (str(attempt["run_id"]), int(attempt["run_attempt"] or 1))
        saved = self.store.get_setting(LIVE_PROGRESS_SETTING)
        with self._live_lock:
            entry = self._live.get(key)
            if entry is None and isinstance(saved, dict) and (saved.get("run_id"), saved.get("run_attempt")) == key:
                entry = {"checked_at": 0, "raw": saved.get("raw"), "progress_at": saved.get("progress_at")}
                self._live = {key: entry}
            if (
                entry is not None
                and entry.get("progress_at") is not None
                and (not isinstance(saved, dict) or saved.get("progress_at") != entry["progress_at"])
            ):
                persist = {
                    "run_id": key[0],
                    "run_attempt": key[1],
                    "raw": entry.get("raw"),
                    "progress_at": entry["progress_at"],
                }
            else:
                persist = None
            due = not self._live_reading and now - (entry or {}).get("checked_at", 0) >= LIVE_PROGRESS_SECONDS
            if due:
                self._live_reading = True
        try:
            if persist is not None:
                self.store.set_setting(LIVE_PROGRESS_SETTING, persist)
        except Exception:
            # The read below still goes ahead; only a reserved reader that never starts would stop polling.
            logging.getLogger(__name__).exception("live progress watermark not saved")
        if not due:
            return

        def read() -> None:
            try:
                report = self.live_progress(*key)
            except Exception as error:  # noqa: BLE001 - observational; a failed read leaves the step phases alone
                report = None
                failure = f"{type(error).__name__}: {str(error)[:200]}"
            else:
                failure = None if report is not None else "no live report for the run"
            # One journal line when reads start failing and one when they recover, not one a minute.
            if (failure is None) != (self._live_failure is None):
                if failure is None:
                    self.log.info("live build progress for run %s attempt %s readable again", *key)
                else:
                    self.log.warning(
                        "live build progress for run %s attempt %s unavailable: %s", key[0], key[1], failure
                    )
                self._live_failure = failure
            with self._live_lock:
                try:
                    current = self._live.get(key, {})
                    raw, progress_at = live_progress_at(report, self.clock())
                    if raw is not None and raw == current.get("raw"):
                        # The same report again: its time was already capped when first seen.
                        progress_at = current.get("progress_at")
                    newest = max(
                        filter(lambda value: value is not None, (current.get("progress_at"), progress_at)), default=None
                    )
                    # Only the head run's entry is kept, and its progress time never goes back.
                    self._live = {
                        key: {
                            "checked_at": self.clock(),
                            "raw": raw if progress_at is not None and progress_at == newest else current.get("raw"),
                            "progress_at": newest,
                        }
                    }
                finally:
                    self._live_reading = False

        try:
            threading.Thread(target=read, name="live-progress", daemon=True).start()
        except Exception:
            with self._live_lock:
                self._live_reading = False
            raise

    def _last_progress_at(self, attempt: dict[str, Any]) -> float:
        """The attempt's last step-level progress, or its live build counts' last advance if later."""
        last = attempt["last_progress_at"]
        if attempt["run_id"]:
            live = self._live.get((str(attempt["run_id"]), int(attempt["run_attempt"] or 1)), {}).get("progress_at")
            if live is not None and live > last:
                return live
        return last

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
        self._set_state("canonical_stale", False)
        head = self._head()
        if head is not None and chain.is_ancestor(self.clone, head["tip"], canonical):
            how = "promoted" if head["expected_parent"] else "external"
            self._landed(head, how, now)
        self._canonical = canonical
        if canonical == previous:
            return
        self._set_state("canonical_sha", canonical)
        self.store.journal(now, "canonical-advance", previous=previous, current=canonical)
        if previous and chain.is_ancestor(self.clone, previous, canonical):
            landed_prs: set[int] = set()
            bors_landed: set[int] = set()
            try:
                trees = {previous: chain.tree_of(self.clone, previous)}
                for commit in chain.chain_commits(self.clone, previous, canonical):
                    source = commit.source_pr
                    empty = len(commit.parents) == 1 and commit.tree == (
                        trees.get(commit.parents[0]) or chain.tree_of(self.clone, commit.parents[0])
                    )
                    trees[commit.sha] = commit.tree
                    if empty:
                        continue  # an empty placeholder lands no contribution
                    if source and source.startswith("forgejo:") and "@" in source:
                        from .mixed_identity import from_source_pr

                        qualified = from_source_pr(source)
                        if self.mixed is not None and qualified.source == self.mixed.profile.forgejo:
                            landed_prs.add(qualified)
                    elif source and source.startswith(f"{self.provenance_repository}#"):
                        landed_prs.add(int(source.rsplit("#", 1)[1]))
                    elif (
                        not source and not self.native_source and (self.mixed is None or self.mixed.primary == "github")
                    ):
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
            self._interacting_landed(landed_prs | bors_landed, now)
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

    # --- admission-time navigation and speculative repair -------------------------------------------

    def _admission_context(self, batch: dict[str, Any], now: float) -> dict[str, Any]:
        members = self._members(batch["id"])
        inputs = {m["input_id"]: self.store.row("inputs", {"id": m["input_id"]}) or {} for m in members}
        key = f"admission_context:{batch['id']}"
        signature = admission_planning.fingerprint(
            [{"schema": 3, "tip": batch["tip"], "names": [r.get("declaration_names") for r in inputs.values()]}]
        )
        prior = self._state(key, {})
        if prior.get("signature") == signature and (not prior.get("retryable") or now < prior.get("retry_after", 0)):
            return prior
        try:
            context = self._planner().context(members, inputs, tip=batch["tip"])
        except Exception as error:  # optional navigation must never strand a reservation or stop CI
            context = {
                "kind": "admission_predictions",
                "tip": batch["tip"],
                "records": [],
                "ownership": [],
                "complete": False,
                "errors": [type(error).__name__],
                "truncated": False,
                "retryable": True,
                "ownership_complete": False,
            }
        context["signature"] = signature
        context["at"] = now
        if context.get("retryable"):
            context["retry_after"] = now + 60
        predictions = self._predicted_collisions(context)
        if predictions and admission_planning.fingerprint(predictions) != prior.get("prediction_digest"):
            self.store.journal(now, "admission-predicted", batch["id"], tip=batch["tip"], collisions=predictions)
            self._count("declaration_collisions_predicted", len(predictions))
        context["prediction_digest"] = admission_planning.fingerprint(predictions)
        self._set_state(key, context)
        return context

    @staticmethod
    def _predicted_collisions(context: dict[str, Any]) -> list[dict[str, Any]]:
        return [r for r in context.get("records", []) if r["kind"] == "declaration_collision"]

    @staticmethod
    def _prediction_task(task: dict[str, Any]) -> bool:
        return any(
            d.get("proactive") for d in loads(task["diagnostics"], []) if d.get("kind") == "admission_predictions"
        )

    def _schedule_predicted_repair(self, head: dict[str, Any], now: float) -> None:
        if self._open_task(head) is not None or self._state("inference_hold"):
            return
        attempt = self._current_attempt(head)
        if attempt is None or attempt["outcome"] != OUTCOME_PENDING:
            return
        context = self._admission_context(head, now)
        collisions = self._predicted_collisions(context)
        if not collisions:
            return
        digest = admission_planning.fingerprint([{"base": head["base"]}, *collisions])
        key = f"admission_repair:{head['id']}:{digest}"
        if self._state(key):
            return
        context = dict(context, proactive=True)
        task_id = self._create_task(head, TASK_REPAIR, head["base"], [context], now, ci_attempt_id=attempt["id"])
        self._set_state(key, {"task_id": task_id, "at": now, "tip": head["tip"]})
        self.store.journal(
            now,
            "admission-repair-started",
            head["id"],
            task_id=task_id,
            attempt_id=attempt["id"],
            collisions=collisions,
        )

    def _record_prediction_result(self, task, batch, comparison, tip, worker_notes, now):
        knowledge = next(d for d in loads(task["diagnostics"], []) if d.get("kind") == "admission_predictions")
        attempt = self._current_attempt(batch) or {}
        edited = [m.source_pr for m in comparison.members if m.provenance == chain.EDITED]
        try:
            notes = loads(worker_notes, {})
        except ValueError:
            notes = {}
        verification = notes.get("admission_verification") if isinstance(notes, dict) else None
        fixed = None
        if (
            isinstance(verification, dict)
            and verification.get("tip") == tip
            and verification.get("checks_passed") is True
        ):
            observed = verification.get("observed_duplicate_names", [])
            if isinstance(observed, list) and all(isinstance(n, str) for n in observed):
                from .mixed_identity import from_source_pr

                edited_numbers = {from_source_pr(pr) for pr in edited}
                fixed = sorted(
                    {
                        r["name"]
                        for r in self._predicted_collisions(knowledge)
                        if r["name"] in observed and any(o["pr"] in edited_numbers for o in r["sources"])
                    }
                )
        self.store.journal(
            now,
            "admission-repair-result",
            batch["id"],
            task_id=task["id"],
            task_kind=task["kind"],
            input_tip=task["tip"],
            attempt_id=attempt.get("id"),
            tip=tip,
            edited=edited,
            collisions=self._predicted_collisions(knowledge),
            observed_ci_outcome=attempt.get("outcome"),
            verified_fixed=fixed,
            verification="authenticated worker scripted duplicate observations and final checks; CI remains required",
        )

    def _adopt_prediction_noop(self, task, claim, batch, tip, retained, notes, now):
        # A conservative prediction may turn out to be harmless. Keep the existing candidate
        # and its CI rather than create an identical version and force another build.
        with self.store.transaction():
            self.store.update(
                "tasks",
                {"id": task["id"]},
                state=TASK_SUBMITTED,
                resolved_at=now,
                resolution="prediction-unchanged",
                submissions=task["submissions"] + 1,
            )
            for active in self._active_claims(batch["id"], task_id=task["id"]):
                self._release_claim(active, "submitted" if active["id"] == claim["id"] else "superseded", now)
            self.store.insert(
                "submissions",
                task_id=task["id"],
                claim_id=claim["id"],
                tip=tip,
                retained_ref=retained,
                accepted=1,
                reason="prediction-unchanged",
                worker_notes=notes,
                created_at=now,
            )
        self.report_inference(claim["worker"], True)
        self._wake.set()
        return Adoption(True, "adopted", task["id"], batch["id"], batch["version"], tip, retained)

    def _clear_admission_state(self, batch_id: int) -> None:
        self.store.execute(
            "DELETE FROM settings WHERE key IN (?, ?) OR key LIKE ?",
            (
                f"state:admission_context:{batch_id}",
                f"state:admission_prepare:{batch_id}",
                f"state:admission_repair:{batch_id}:%",
            ),
        )

    def _finish_prediction_pass(self, head, task, attempt, now):
        with self.store.transaction():
            self.store.update(
                "tasks", {"id": task["id"]}, state=TASK_SUPERSEDED, resolved_at=now, resolution="prediction-ci-passed"
            )
            for claim in self._active_claims(head["id"], task_id=task["id"]):
                self._release_claim(claim, "superseded", now)
            self._promote(head, attempt, now)

    def _update_prediction_verdict(self, head, task, attempt, now):
        if attempt["outcome"] == OUTCOME_SOURCE_FAILURE:
            knowledge = [
                dict(d, proactive=False, was_proactive=True)
                for d in loads(task["diagnostics"], [])
                if d.get("kind") == "admission_predictions"
            ]
            self.store.update(
                "tasks", {"id": task["id"]}, diagnostics=dumps([*loads(attempt["diagnostics"], []), *knowledge])
            )
        elif attempt["outcome"] in (OUTCOME_INFRASTRUCTURE, OUTCOME_SUPERSEDED):
            self._ci(head, now)  # speculative work must not disable the controller's retry policy

    # --- insertion ---------------------------------------------------------------------------------

    def _waiting(self) -> list[dict[str, Any]]:
        rows = self.store.rows("inputs", {"repository": self.repository, "state": INPUT_WAITING})
        from .mixed_identity import SourceNumber

        rows = [
            row
            for row in rows
            if (not isinstance(row["number"], SourceNumber) or self.mixed is not None and self.mixed.mode == "admit")
            # An emergency request is drawn only into its own batch (`_emergency`).
            and row["urgent"] != URGENT_EMERGENCY
        ]
        rows.sort(key=lambda row: (-row["urgent"], -row["priority"], row["approved_at"], row["id"]))
        return rows

    def _insertion_passes(self, now: float) -> None:
        """Probe each waiting input at most once per open batch per tick, one age-windowed random draw per pass.

        Open batches are tried oldest first. An input that conflicted with this very tip would conflict
        again, and a textual conflict is with content already in the chain, so appending members cannot
        resolve it: an input whose recorded conflict is against a batch's current base is not probed there
        again but against the next open batch, created on the canonical commit (never on another batch's
        tip) while fewer than `open_batch_limit` are open. An input that conflicts with every open batch
        waits for a base to change (a new open batch or a rebase). A batch a `prepare` task holds is
        skipped the same way, so inflow continues into the next open batch while it is prepared.

        The passes stop once `insertion_probe_budget_seconds` of wall time is spent (each probe is a fetch
        and a git replay onto a batch's tip, run without the controller lock, see `_probe`); the remaining
        inputs wait for the next tick, which the loop is woken to run at once. `probed` is per tick and
        the draw's age bound carries over in `passes_waited`, so a paused tick starves nobody.
        """
        if self._inert_barrier() is not None:
            self._reasons.append("insertion waits behind retained work from a disabled source")
            return
        probed: set[tuple[int, int]] = set()  # (input, batch) pairs probed in this tick
        started = time.monotonic()
        budget = float(self.setting("insertion_probe_budget_seconds"))
        # Control-file inputs go to the lane first and are never probed into an ordinary batch.
        self._control_lane_insertion(now, probed, started, budget)
        while self._ordinary_waiting():
            if probed and time.monotonic() - started >= budget:
                self._reasons.append(f"insertion paused after {len(probed)} probes (budget)")
                self._wake.set()
                break
            batches = self._ensure_open_batches(now)
            if batches is None:
                break
            remaining = self._ordinary_waiting()
            target: dict[str, Any] | None = None
            eligible: list[dict[str, Any]] = []
            for batch in batches:
                if any(c["operation"] in BATCH_TASK_OPERATIONS for c in self._active_claims(batch["id"])):
                    # A `prepare` task, or the batch's rebase onto `main`, holds this batch: insertion goes
                    # on to the next open batch (created on the canonical commit when the limit allows).
                    self._note_reason(f"open batch {batch['id']} is being prepared; insertion uses the next one")
                    continue
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
                # An out-of-scope input already tried on canonical cannot be
                # helped by another empty batch on that same tree. A primary
                # reversal clears the wait; a new canonical allows one retry.
                remaining = [
                    row
                    for row in remaining
                    if not (
                        row["last_probe"].startswith("source-ci-scope:") and row["last_probe_tip"] == self._canonical
                    )
                ]
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
        if row["last_probe"] == MISSING_SOURCE_IN_FLIGHT and row["last_probe_base"] == batch["base"]:
            return False  # the source it imports is still in flight; no batch on this base has it
        return (
            row["last_probe_tip"] != batch["tip"]
            and not self._conflicted_with(row, batch)
            and (self.admission is None or self.admission.is_current(row["number"], row["head_sha"]))
        )

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
        for batch in self._unsealed_batches():
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
        and its base is stale. An empty lane batch goes when its base is stale or no control input waits."""
        for position, batch in enumerate(self._unsealed_batches()):
            if self._members(batch["id"]):
                continue
            if position > 0:
                self._remove_batch(batch, "idle", now)
            elif not self._ordinary_waiting() and self._canonical is not None and batch["base"] != self._canonical:
                self._remove_batch(batch, "idle", now)
        lane = self._lane_batch() if self._control_lane_active() else None
        if lane is not None and not self._members(lane["id"]):
            if lane["base"] != self._canonical or not any(self._control_input(row) for row in self._waiting()):
                self._remove_batch(lane, "idle", now)

    def _remove_batch(self, batch: dict[str, Any], reason: str, now: float) -> None:
        with self.store.transaction():
            self.store.update("batches", {"id": batch["id"]}, state=BATCH_REMOVED)
            self.store.journal(now, "batch-removed", batch["id"], reason=reason)
            self._clear_admission_state(batch["id"])
        self._cancel_superseded_runs(batch["id"], now, reason=f"batch removed ({reason})")

    def _probe(self, row: dict[str, Any], batch: dict[str, Any], now: float) -> bool:
        """Reserve the input, claim the open batch, replay the squash onto its tip; commit or roll back.

        The fetch of the head and the replay run without the controller lock (the reservation and the
        `insert` claim stand for it meanwhile); the outcome is recorded under the lock only if the input
        is still reserved (no event withdrew it meanwhile) and the batch has neither moved nor been
        claimed by a `prepare` task, else the input goes back to waiting.

        A worker's claim on the batch's `pre_rebase` task does not stop insertion: the task covers the
        members it was created with, and members appended since are replayed onto its result.
        """
        if [c for c in self._active_claims(batch["id"]) if c["task_id"] is None]:
            self._reasons.append(f"open batch {batch['id']} is claimed")
            return False
        with self.store.transaction():
            self.store.update(
                "inputs", {"id": row["id"]}, state=INPUT_RESERVED, batch_id=batch["id"], probes=row["probes"] + 1
            )
            claim = self._take_claim(batch, "insert", now)
        member = chain.Member(
            self._member_repository(row["number"]),
            int(row["number"]),
            row["head_sha"],
            title=row["title"],
            description=row["description"],
            author="" if self.native_source or isinstance(row["number"], SourceNumber) else row["author"],
            coauthors=None if loads(row["coauthors"]) is None else tuple(loads(row["coauthors"])),
        )
        chain_id = f"probe-{batch['id']}-{secrets.token_hex(4)}"
        failure: chain.ChainError | None = None
        result: chain.ChainResult | None = None
        gaps: list[tuple[str, str]] = []
        scan_unavailable: str | None = None
        scope_supported = True
        held: str | None = None
        try:
            with self._lock.unlocked():
                if not self._fetch_head(row["number"], row["head_sha"]):
                    raise chain.ChainError(f"accepted head {row['head_sha']} is not available")
                result = chain.build(self.clone, batch["tip"], [member], chain_id=chain_id)
                gaps, scan_unavailable, scope_supported = self._replay_preflight(batch["base"], batch["tip"], result)
                check = getattr(self.admission, "insertion_check", None)
                if result.status == chain.CLEAN and callable(check):
                    held = check(row["number"], row["head_sha"])
                    held = held if isinstance(held, str) else None  # a reason, or nothing to hold
        except chain.ChainError as error:
            failure = error
        finally:
            chain.delete_chain_ref(self.clone, chain_id)
        fresh = self.store.row("inputs", {"id": row["id"]})
        if fresh is None or fresh["state"] != INPUT_RESERVED:
            # Withdrawn while the probe ran (closed, a new head, unapproved): it is not inserted.
            with self.store.transaction():
                if fresh is not None and fresh["batch_id"] == batch["id"]:
                    self.store.update("inputs", {"id": row["id"]}, batch_id=None)
                self._release_claim(claim, "input-withdrawn", now)
            return False
        if self._label_blocked(fresh):
            # A blocking label arrived while the probe ran: it waits, unplaced, until the label goes.
            with self.store.transaction():
                self.store.update(
                    "inputs", {"id": row["id"]}, state=INPUT_WAITING, batch_id=None, last_probe="blocked-label"
                )
                self._release_claim(claim, "blocked-label", now)
            return False
        if failure is not None:
            with self.store.transaction():
                self.store.update(
                    "inputs",
                    {"id": row["id"]},
                    state=INPUT_WAITING,
                    batch_id=None,
                    last_probe=f"error: {failure}",
                    last_probe_base="",
                )
                self._release_claim(claim, "probe-error", now)
                self.store.journal(now, "probe-error", batch["id"], number=row["number"], error=str(failure))
            return False
        assert result is not None
        row = fresh
        if isinstance(row["number"], SourceNumber) and not self.setting("forgejo_placement_enabled"):
            # Forgejo placement was paused while the probe ran: the input waits with the others.
            with self.store.transaction():
                self.store.update(
                    "inputs", {"id": row["id"]}, state=INPUT_WAITING, batch_id=None, last_probe="placement-paused"
                )
                self._release_claim(claim, "placement-paused", now)
            return False
        current = self.store.row("batches", {"id": batch["id"]})
        prepared = any(c["operation"] in BATCH_TASK_OPERATIONS for c in self._active_claims(batch["id"]))
        if (
            current is None
            or current["version"] != batch["version"]
            or current["state"] != BATCH_OPEN
            or current["sealed"]
            or prepared
        ):
            # The batch moved while the probe ran without the lock (an adoption, a removal), or a
            # `prepare` task claimed it: the input waits for the next pass.
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
            selection = self._admission_context(batch, now)
            interacting = sorted(
                {entry["pr"] for entry in selection.get("ownership", []) if any(p in paths for p in entry["paths"])}
            )
            evidence = loads(row.get("evidence"), {})
            evidence["merge_selection"] = [
                *evidence.get("merge_selection", []),
                {
                    "kind": "selection_conflict",
                    "pr": row["number"],
                    "batch_id": batch["id"],
                    "base": batch["base"],
                    "tip": batch["tip"],
                    "paths": paths,
                    "members": sorted(set(interacting)),
                    "interacting_complete": selection.get("ownership_complete", False),
                    "at": now,
                },
            ][-8:]
            with self.store.transaction():
                self.store.update("inputs", {"id": row["id"]}, evidence=dumps(evidence))
                if against_main:
                    self.store.update(
                        "inputs",
                        {"id": row["id"]},
                        state=INPUT_CANCELLED,
                        batch_id=None,
                        reason="conflicts-with-main",
                        last_probe=dumps(paths),
                    )
                    if self.admission is not None:
                        self.admission.withdraw(row["number"], row["head_sha"])
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
                    now,
                    "probe-conflict",
                    batch["id"],
                    number=row["number"],
                    paths=paths,
                    against_main=against_main,
                    interacting=sorted(set(interacting)),
                )
            return False
        record = result.commits[0]
        if self._source_ci_required(row["number"], batch) and not scope_supported:
            reason = (
                "source-ci-scope: native candidate needs a compatible batch or changes within the native path policy"
                if isinstance(row["number"], SourceNumber)
                else "source-ci-scope: human control-file changes require GitHub primary and a GitHub-only batch"
            )
            with self.store.transaction():
                open_ids = {str(b["id"]) for b in self._open_batches()}
                conflicts = {k: v for k, v in loads(row["probe_conflicts"], {}).items() if k in open_ids}
                if self._members(batch["id"]):
                    conflicts[str(batch["id"])] = batch["base"]
                self.store.update(
                    "inputs",
                    {"id": row["id"]},
                    state=INPUT_WAITING,
                    batch_id=None,
                    last_probe=reason,
                    last_probe_tip=batch["tip"],
                    last_probe_base=batch["base"],
                    probe_conflicts=dumps(conflicts),
                )
                self._release_claim(claim, "source-ci-scope", now)
                self.store.journal(now, "probe-source-ci-scope", batch["id"], number=row["number"], reason=reason)
            return False
        if scan_unavailable is not None:
            self.store.journal(
                now, "probe-import-scan-unavailable", batch["id"], number=row["number"], reason=scan_unavailable
            )
        if gaps:
            # The replay is clean but the prospective tree has an import with no source that the
            # current tip does not have: this PR removed the source or added the import. CI's
            # import-header preflight would fail the whole head for it; refuse the PR instead,
            # the way a conflict with the canonical branch is refused.
            changed = self._changed_paths(record.sha)
            listed = [{"path": path, "module": module, "changed": path in changed} for path, module in gaps[:8]]
            in_flight = self._providers_in_flight(batch, gaps)
            if in_flight is not None:
                # Every missing source is in flight or already on a newer canonical base. Preserve
                # the input until its batch base moves; merely appending on this old base cannot
                # supply it. The canonical revision distinguishes refresh waits from live providers.
                with self.store.transaction():
                    self.store.update(
                        "inputs",
                        {"id": row["id"]},
                        state=INPUT_WAITING,
                        batch_id=None,
                        last_probe=MISSING_SOURCE_IN_FLIGHT,
                        last_probe_tip=batch["tip"],
                        last_probe_base=batch["base"],
                    )
                    self._release_claim(claim, "missing-source-in-flight", now)
                    self.store.journal(
                        now,
                        "probe-missing-source-waiting",
                        batch["id"],
                        number=row["number"],
                        gaps=listed,
                        providers_in=in_flight,
                        canonical=self._canonical,
                    )
                return False
            evidence = loads(row.get("evidence"), {})
            evidence["merge_selection"] = [
                *evidence.get("merge_selection", []),
                {
                    "kind": "missing_source",
                    "pr": row["number"],
                    "batch_id": batch["id"],
                    "base": batch["base"],
                    "tip": batch["tip"],
                    "gaps": listed,
                    "at": now,
                },
            ][-8:]
            try:
                evidence[MISSING_SOURCE_WATCH] = self._dependency_watch([(row, listed)])[0]
            except chain.ChainError:
                evidence[MISSING_SOURCE_WATCH] = ""  # the next recheck pass records it
            with self.store.transaction():
                self.store.update(
                    "inputs",
                    {"id": row["id"]},
                    evidence=dumps(evidence),
                    state=INPUT_CANCELLED,
                    batch_id=None,
                    reason="missing-source",
                    last_probe=dumps(listed),
                    last_probe_base="",
                )
                if self.admission is not None:
                    self.admission.withdraw(row["number"], row["head_sha"])
                self._missing_source_request(row, listed)
                self._release_claim(claim, "missing-source", now)
                self._count("missing_source_refusals")
                self.store.journal(
                    now, "probe-missing-source", batch["id"], number=row["number"], tip=batch["tip"], gaps=listed
                )
            return False
        version = batch["version"] + 1
        with self.store.transaction():
            if held is not None:
                # The inputs admission evaluated have changed or could not be read: evaluate again.
                self.store.update(
                    "inputs", {"id": row["id"]}, state=INPUT_WAITING, batch_id=None, last_probe="admission-recheck"
                )
                self._release_claim(claim, "admission-recheck", now)  # the check re-dirtied the PR if needed
                self._count("insertion_checks_held")
                self.store.journal(
                    now, "insertion-check-held", batch["id"], number=row["number"], head=row["head_sha"], reason=held
                )
                return False
            if self.admission is not None and not self.admission.is_current(row["number"], row["head_sha"]):
                self.store.update(
                    "inputs", {"id": row["id"]}, state=INPUT_WAITING, batch_id=None, last_probe="admission-pending"
                )
                self._release_claim(claim, "admission-pending", now)
                return False
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
                no_repair=row["no_repair"],
            )
            # An ordinary insertion never makes an emergency batch: a request that flagged the input
            # while this probe ran makes the batch urgent at most; the lane takes the input from here.
            urgent = (
                batch["urgent"] if batch["urgent"] == URGENT_EMERGENCY else max(batch["urgent"], min(row["urgent"], 1))
            )
            self.store.update("batches", {"id": batch["id"]}, version=version, tip=result.tip, urgent=urgent)
            self._record_tip(batch["id"], version, result.tip, now)
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
            if self.setting("admission_insertion_check") and not isinstance(row["number"], SourceNumber):
                self._count("insertion_checks_passed")
            self.store.journal(
                now,
                "inserted",
                batch["id"],
                number=row["number"],
                version=version,
                commit=record.sha,
                empty=record.empty,
            )
        self._admission_context(self.store.row("batches", {"id": batch["id"]}) or batch, now)
        return True

    def _replay_preflight(
        self, base: str, tip: str, result: chain.ChainResult
    ) -> tuple[list[tuple[str, str]], str | None, bool]:
        """The checks a clean replay onto `tip` (on `base`) gets before insertion, run without the lock:
        the new import gaps, why the import scan was unavailable, and whether the source-candidate scope
        supports the candidate (`_source_ci_required` says whether that applies)."""
        if result.status != chain.CLEAN:
            return [], None, True
        gaps, scan_unavailable = self._new_import_gaps(tip, result)
        scope_supported = True
        if self.mixed is not None and self.mixed.mode == "admit":
            scope_supported = self.mixed.supports_candidate(base, result.tip)
        return gaps, scan_unavailable, scope_supported

    def _source_ci_required(self, number: Any, batch: dict[str, Any] | None) -> bool:
        """Whether inserting `number` into `batch` routes the batch's CI through source-candidate CI,
        whose scope then restricts it; `batch` None is a new batch of that member alone."""
        if self.mixed is None or self.mixed.mode != "admit":
            return False
        if isinstance(number, SourceNumber):
            return True
        if batch is not None:
            return self._candidate_coordinator(batch_id=batch["id"]) is not None
        return self.candidate_ci is not None or (self.mixed_ci is not None and self.mixed.primary == "forgejo")

    def _planner(self) -> admission_planning.Planner:
        if not hasattr(self, "_admission_planner"):
            self._admission_planner = admission_planning.Planner(self.clone)
        return self._admission_planner

    def _new_import_gaps(self, tip: str, result: chain.ChainResult) -> tuple[list[tuple[str, str]], str | None]:
        """Project imports with no source in the prospective tree that the current tip does not have.

        Comparing the two trees charges every new gap to the PR being inserted: a gap already on the
        batch's base appears in both scans, while adding an importer after a batch-mate removed its
        source creates a new gap. Returns the sorted new gaps, or `([], reason)` when either scan is unavailable,
        in which case the insertion proceeds and the CI preflight remains the authority.
        """
        planner = self._planner()
        before, why = admission_planning.import_gaps(planner, chain.tree_of(self.clone, tip))
        if before is None:
            return [], why
        after, why = admission_planning.import_gaps(planner, chain.tree_of(self.clone, result.tip))
        if after is None:
            return [], why
        return sorted(after - before), None

    def _providers_in_flight(self, batch: dict[str, Any], gaps: list[tuple[str, str]]) -> list[int] | None:
        """Batches or the current canonical tree supplying every missing source, else None.

        A source the prospective PR removes from this batch cannot be supplied by waiting. A
        different batch merely inheriting that source is not an incoming dependency either: after
        it lands, replaying the removal (or a batch-mate's removal) still strands the importer.
        """
        candidates = [b for b in [self._head(), *self._open_batches()] if b is not None and b["id"] != batch["id"]]
        found: list[int] = []
        for _, module in gaps:
            provider = module.replace(".", "/") + ".lean"
            if self._tree_has(batch["tip"], provider) or self._tree_has(batch["base"], provider):
                return None
            if self._canonical and self._tree_has(self._canonical, provider):
                continue  # an older open batch will acquire this source when its base refreshes
            holder = next(
                (
                    b
                    for b in candidates
                    if b["tip"] and self._tree_has(b["tip"], provider) and not self._tree_has(b["base"], provider)
                ),
                None,
            )
            if holder is None:
                return None
            if holder["id"] not in found:
                found.append(holder["id"])
        return found

    def _tree_has(self, commit: str, path: str) -> bool:
        """Whether `commit` carries a blob at `path` (`rev:path` cannot take a `^{blob}` peel)."""
        return chain._git(self.clone, "cat-file", "-e", f"{commit}:{path}").returncode == 0

    def _changed_paths(self, sha: str) -> set[str]:
        planner = self._planner()
        try:
            planner.load([sha])
        except (ValueError, UnicodeError, subprocess.SubprocessError):
            return set()
        return {path for path, _ in planner.changes.get(sha, [])}

    def _missing_source_request(self, row: dict[str, Any], gaps: list[dict[str, Any]]) -> None:
        if not self.setting("publication_enabled"):
            return
        removal = any(not gap["changed"] for gap in gaps)
        lines = []
        for gap in gaps:
            provider = gap["module"].replace(".", "/") + ".lean"
            if gap["changed"]:
                lines.append(f"- `{gap['path']}` imports `{gap['module']}`, but no source provides it (`{provider}`).")
            else:
                lines.append(
                    f"- `{gap['path']}` still imports `{gap['module']}`, whose source `{provider}` this PR removes."
                )
        if removal:
            summary = "This PR removes or renames a module that other sources still import."
            next_step = "Keep the module or update every importer, then approve it again."
        else:
            summary = "This PR imports a module that no source provides."
            next_step = "Fix the import, then approve it again."
        body = summary + "\n\n" + "\n".join(lines) + "\n\n" + next_step
        self._enqueue(
            "comment",
            f"missing-source:{row['number']}:{row['head_sha']}",
            {
                "number": row["number"],
                "body": body,
                "marker": f"<!-- {COMMENT_MARKER} missing-source:{row['head_sha']} -->",
            },
        )

    def _blob_ids(self, specs: list[str]) -> dict[str, str]:
        """The blob id at each `commit:path` in `specs`, "" where there is none, in one `cat-file` call."""
        specs = sorted(set(specs))
        if not specs:
            return {}
        result = chain._git(
            self.clone, "cat-file", "--batch-check=%(objectname) %(objecttype)", input="\n".join(specs) + "\n"
        )
        lines = result.stdout.splitlines() if result.returncode == 0 else []
        if len(lines) != len(specs):
            raise chain.ChainError(f"git cat-file --batch-check failed: {result.stderr.strip()}")
        ids = {}
        for spec, line in zip(specs, lines, strict=True):
            fields = line.split()
            ids[spec] = fields[0] if len(fields) == 2 and fields[1] == "blob" else ""
        return ids

    def _dependency_watch(
        self,
        refusals: list[tuple[dict[str, Any], list[dict[str, Any]]]],
        *,
        at: dict[int, str] | None = None,
        batches: list[dict[str, Any]] | None = None,
    ) -> list[str]:
        """The dependency fingerprint of each `(input, gaps)` refusal, for `MISSING_SOURCE_WATCH`.

        For each gap it covers the blobs at the importer and at the missing source on the canonical
        commit (or `at[input id]`), and the distinct such pairs at active batch tips (`batches`, by default
        the head and the open batches) that differ from the canonical pair. A source that lands, enters
        or leaves a batch, or an importer that changes on `main` or in a batch, changes it; landings and
        insertions that touch neither path do not.
        """
        if batches is None:
            batches = [b for b in [self._head(), *self._open_batches()] if b is not None and b["tip"]]
        canonical = self._canonical or ""
        commits = {(at or {}).get(row["id"], canonical) for row, _ in refusals} | {b["tip"] for b in batches}
        specs = [
            f"{commit}:{path}"
            for _, gaps in refusals
            for gap in gaps
            for path in (gap["path"], gap["module"].replace(".", "/") + ".lean")
            for commit in commits
            if commit
        ]
        ids = self._blob_ids(specs)
        prints = []
        for row, gaps in refusals:
            commit = (at or {}).get(row["id"], canonical)
            facts = []
            for gap in gaps:
                provider = gap["module"].replace(".", "/") + ".lean"
                here = [ids.get(f"{commit}:{gap['path']}", ""), ids.get(f"{commit}:{provider}", "")]
                elsewhere = {
                    (ids.get(f"{b['tip']}:{gap['path']}", ""), ids.get(f"{b['tip']}:{provider}", "")) for b in batches
                }
                facts.append(
                    {
                        "path": gap["path"],
                        "module": gap["module"],
                        "canonical": here,
                        "batches": sorted(list(pair) for pair in elsewhere if list(pair) != here),
                    }
                )
            prints.append(admission_planning.fingerprint(facts))
        return prints

    def _current_refusals(self) -> list[dict[str, Any]]:
        """Each PR's `missing-source` refusal that is still its newest input and has no live one."""
        return self.store.query(
            "SELECT i.* FROM inputs i WHERE i.repository = ? AND i.state = ? AND i.reason = 'missing-source' "
            "AND NOT EXISTS (SELECT 1 FROM inputs j WHERE j.repository = i.repository AND j.number = i.number "
            "AND j.source_identity = i.source_identity AND (j.id > i.id OR j.state IN (?, ?, ?))) ORDER BY i.id",
            (self.repository, INPUT_CANCELLED, INPUT_WAITING, INPUT_RESERVED, INPUT_INSERTED),
        )

    def _native_admission_open(self) -> bool:
        return bool(self.mixed is not None and self.mixed.mode == "admit" and self.setting("forgejo_admission_enabled"))

    def _recheck_missing_sources(self, now: float) -> None:
        """Re-admit `missing-source` refusals whose dependency fingerprint changed, at their original age.

        A refused PR imports a module nothing provides, or removes one something still imports. Landing
        the provider, an importer dropping the import, or a batch introducing the provider can resolve
        that without a new push, so whenever the canonical commit or an active batch's tip moves, each
        newest refusal of a PR still at its refused head is fingerprinted again (`_dependency_watch`).
        One that changed is handed to admission (`readmit`), which reads the PR again and restores the
        input only if it is still admissible (`restore_missing_source`); without an admission port the
        live PR is read here. A restored input is probed as any waiting input, which refuses it again
        if a gap remains; the refusal comment is keyed by head, so that posts nothing new. Refusals
        recorded before the fingerprint existed compare against the base of the batch that refused
        them, without batches.
        """
        if self._canonical is None:
            return
        batches = [b for b in [self._head(), *self._open_batches()] if b is not None]
        signature = (
            self._canonical,
            tuple(sorted((b["id"], b["tip"]) for b in batches)),
            self._native_admission_open(),
        )
        if signature == self._missing_source_signature:
            return
        refusals: list[tuple[dict[str, Any], list[dict[str, Any]]]] = []
        baseline: dict[int, str] = {}
        for row in self._current_refusals():
            if isinstance(row["number"], SourceNumber) and not self._native_admission_open():
                continue
            gaps = loads(row["last_probe"], None)
            if not isinstance(gaps, list) or not gaps:
                continue
            if not all(isinstance(g, dict) and g.get("path") and g.get("module") for g in gaps):
                continue
            evidence = loads(row["evidence"], {})
            if MISSING_SOURCE_WATCH not in evidence:
                refused = [e for e in evidence.get("merge_selection", []) if e.get("kind") == "missing_source"]
                if not refused or not refused[-1].get("base"):
                    continue
                baseline[row["id"]] = refused[-1]["base"]
            refusals.append((row, gaps))
        if not refusals:
            self._missing_source_signature = signature
            return
        try:
            prints = self._dependency_watch(refusals)
            legacy = [(row, gaps) for row, gaps in refusals if row["id"] in baseline]
            before = dict(
                zip(
                    [row["id"] for row, _ in legacy],
                    self._dependency_watch(legacy, at=baseline, batches=[]),
                    strict=True,
                )
            )
        except chain.ChainError as error:
            self._note_reason(f"missing-source recheck unavailable: {error}")
            return
        self._missing_source_signature = signature
        for (row, gaps), fingerprint in zip(refusals, prints, strict=True):
            evidence = loads(row["evidence"], {})
            recorded = before[row["id"]] if row["id"] in before else evidence.get(MISSING_SOURCE_WATCH)
            if fingerprint != recorded:
                self._readmit_missing_source(row, gaps, fingerprint, now)
            elif row["id"] in before:
                evidence[MISSING_SOURCE_WATCH] = fingerprint  # from now on, active batches count
                self.store.update("inputs", {"id": row["id"]}, evidence=dumps(evidence))

    def _readmit_missing_source(
        self, row: dict[str, Any], gaps: list[dict[str, Any]], fingerprint: str, now: float
    ) -> None:
        """Hand a `missing-source` refusal whose dependencies changed back to admission
        (`_recheck_missing_sources`); without an admission port, restore it here if the live PR allows."""
        evidence = loads(row["evidence"], {})
        evidence[MISSING_SOURCE_WATCH] = fingerprint
        if self.admission is None:
            pull, reason = self.validate_pull(self.repository, row["number"], row["head_sha"])
            if reason is not None and reason.startswith("fetch-failed"):
                self._note_reason(f"missing-source recheck of #{row['number']} waits for GitHub: {reason}")
                self._missing_source_signature = None
                return
            with self.store.transaction():
                self.store.update("inputs", {"id": row["id"]}, evidence=dumps(evidence))
                self.store.journal(
                    now, "missing-source-recheck", number=row["number"], head=row["head_sha"], gaps=gaps[:8]
                )
                if reason is not None:
                    # Closed, drafted, retargeted or moved on: only a new approval brings it back.
                    self._drop_missing_source(row, reason, now)
                    return
                self.store.update("inputs", {"id": row["id"]}, reason=MISSING_SOURCE_RECHECK)
                self._restore_missing_source(row["number"], row["head_sha"], pull, now)
            return
        with self.store.transaction():
            if not self.admission.readmit(row["number"], row["head_sha"]):
                return
            self.store.update("inputs", {"id": row["id"]}, reason=MISSING_SOURCE_RECHECK, evidence=dumps(evidence))
            self.store.journal(now, "missing-source-recheck", number=row["number"], head=row["head_sha"], gaps=gaps[:8])

    def restore_missing_source(self, number: Any, head: str, pull: Any) -> str | None:
        """Called by admission, under the controller lock, when its fresh read of `pull` would admit
        `head`: a refusal `_recheck_missing_sources` handed back waits again at its original age.
        Returns the disposition, or None when the PR has no such refusal at `head`."""
        return self._restore_missing_source(number, head, pull, self.clock())

    def _restore_missing_source(self, number: Any, head: str, pull: Any, now: float) -> str | None:
        current = self._input_for(number)
        if (
            current is None
            or current["state"] != INPUT_CANCELLED
            or current["reason"] != MISSING_SOURCE_RECHECK
            or current["head_sha"] != head
        ):
            return None
        blocked = self._blocking_labels(pull)
        if blocked:
            # The refusal stays pending: admission restores it once the labels go.
            self.store.journal(now, "approval-refused", number=number, head=head, labels=blocked)
            return f"blocked-label: {', '.join(blocked)}"
        with self.store.transaction():
            self.store.update(
                "inputs",
                {"id": current["id"]},
                state=INPUT_WAITING,
                reason="",
                batch_id=None,
                passes_waited=0,
                last_probe="missing-source-recheck",
                last_probe_tip="",
                last_probe_base="",
                probe_conflicts="{}",
            )
            self._count("missing_source_rechecks")
            self.store.journal(now, "missing-source-readmitted", number=number, head=head)
        self._wake.set()
        return "readmitted"

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

    # --- the emergency lane ------------------------------------------------------------------------
    #
    # An admin's request (`request_emergency`) flags a live input with `urgent = URGENT_EMERGENCY` and
    # `evidence.emergency`. `_emergency` builds one single-member batch E for the oldest request
    # (`batches.urgent = URGENT_EMERGENCY`), which ordinary scheduling does not see (`_staging_batches`)
    # and `_seal` makes the next head ahead of every open batch. E never evicts a running head. A PR
    # already inserted in an open batch B is built into E as well; B keeps its slot, which no longer
    # acts for the input (`_displaced`). Every way E's member leaves E goes through `_emergency_exit`,
    # every way E ends (landing aside) through `_end_emergency`, and E's runs are cancelled through
    # durable intents (`_settle_cancel_intents`).

    def _active_members(self, batch: dict[str, Any]) -> list[dict[str, Any]]:
        return [m for m in self._members(batch["id"]) if not m["withdrawn"]]

    @staticmethod
    def _emergency_requested(row: dict[str, Any]) -> bool:
        return row["urgent"] == URGENT_EMERGENCY and "emergency" in loads(row["evidence"], {})

    def _flagged_inputs(self) -> list[dict[str, Any]]:
        """The live inputs with an emergency request, oldest request first."""
        rows = self.store.rows(
            "inputs",
            {
                "repository": self.repository,
                "state": [INPUT_WAITING, INPUT_RESERVED, INPUT_INSERTED],
                "urgent": URGENT_EMERGENCY,
            },
        )
        rows = [row for row in rows if self._emergency_requested(row)]
        rows.sort(key=lambda row: (loads(row["evidence"], {})["emergency"].get("requested_at") or 0, row["id"]))
        return rows

    def _emergency_input(self, batch: dict[str, Any]) -> int | None:
        """The id of the input emergency batch `batch` was built for."""
        members = self.store.rows("members", {"batch_id": batch["id"]}, order="id", limit=1)
        if members:
            return members[0]["input_id"]
        for row in self._flagged_inputs():
            if loads(row["evidence"], {})["emergency"].get("batch_id") == batch["id"]:
                return row["id"]
        return None

    def _emergency_behind(self, row: dict[str, Any]) -> Any:
        """The PR whose emergency request is served before `row`'s, or None when it is served next."""
        order: list[dict[str, Any]] = []
        batch = self._emergency_batch()
        if batch is not None and (input_id := self._emergency_input(batch)) is not None:
            order.append(self.store.row("inputs", {"id": input_id}) or {})
        order += [r for r in self._flagged_inputs() if r["id"] not in {o.get("id") for o in order}]
        ids = [o.get("id") for o in order]
        position = ids.index(row["id"]) if row["id"] in ids else len(order)
        return order[position - 1]["number"] if position > 0 else None

    def _clear_emergency_request(self, row: dict[str, Any]) -> None:
        """Forget the input's emergency request: no `evidence.emergency`, ordinary urgency."""
        evidence = loads(row["evidence"], {})
        evidence.pop("emergency", None)
        values: dict[str, Any] = {"evidence": dumps(evidence)}
        if row["urgent"] == URGENT_EMERGENCY:
            values["urgent"] = 1
        self.store.update("inputs", {"id": row["id"]}, **values)

    def _displaced(self, batch: dict[str, Any], member: dict[str, Any]) -> bool:
        """The member's input now belongs to an emergency batch other than `batch`: the slot never acts
        for the input (no input write, approval withdrawal or PR effect, and it does not land)."""
        row = self.store.row("inputs", {"id": member["input_id"]})
        if row is None or row["batch_id"] in (None, batch["id"]):
            return False
        owner = self.store.row("batches", {"id": row["batch_id"]})
        return owner is not None and owner["urgent"] == URGENT_EMERGENCY

    def _emergency_exit(self, batch: dict[str, Any], member: dict[str, Any], now: float) -> bool:
        """The one path by which a member leaves the emergency batch that owns its input; True when the
        input went back to its live displaced slot (the caller then leaves input, approval and PR alone).

        Called inside the caller's transaction. The request is over either way (ordinary urgency, no
        `evidence.emergency`). When the slot in batch B the PR was displaced from is still live, the input
        is `inserted` in B again, E's member is withdrawn and the author is told once; otherwise the
        caller's own behaviour applies (a withdrawal cancels, a return waits at the original age)."""
        if batch["urgent"] != URGENT_EMERGENCY:
            return False
        row = self.store.row("inputs", {"id": member["input_id"]})
        if row is None or row["batch_id"] != batch["id"]:
            return False
        record = loads(row["evidence"], {}).get("emergency") or {}
        displaced = record.get("displaced") or {}
        slot = self.store.row("members", {"id": displaced["member_id"]}) if displaced.get("member_id") else None
        owner = self.store.row("batches", {"id": displaced["batch_id"]}) if displaced.get("batch_id") else None
        live = (
            slot is not None
            and owner is not None
            and owner["state"] in (BATCH_OPEN, BATCH_HEAD)
            and slot["batch_id"] == owner["id"]
            and slot["input_id"] == row["id"]
            and not slot["removed"]
            and not slot["withdrawn"]
        )
        self._clear_emergency_request(row)
        self.store.journal(
            now, "emergency-exit", batch["id"], number=member["number"], returned_to=owner["id"] if live else None
        )
        if not live:
            return False
        assert owner is not None
        self.store.update("inputs", {"id": row["id"]}, state=INPUT_INSERTED, batch_id=owner["id"])
        self.store.update("members", {"id": member["id"]}, withdrawn=1)
        if self.setting("publication_enabled"):
            marker = f"<!-- {COMMENT_MARKER} emergency-returned:{self._source_pr(member['number'])}:{batch['id']} -->"
            body = (
                f"The emergency lane did not land this PR (batch {batch['id']}); it stays in batch {owner['id']} "
                "and lands with it as usual."
            )
            self._enqueue(
                "comment",
                f"emergency-returned:{row['id']}:{batch['id']}",
                {"number": member["number"], "body": body + "\n\n" + marker, "marker": marker},
            )
        return True

    def _emergency_ready(self) -> dict[str, Any] | None:
        """The open emergency batch when `_seal` may make it the head: it has an active member and is not
        retained for a disabled source. It is staged ahead of every open batch, so the only barriers that
        can apply to it are its own and an inert head, which stops the tick before sealing."""
        batch = self._emergency_batch()
        if (
            batch is None
            or batch["state"] != BATCH_OPEN
            or not self._active_members(batch)
            or self._mixed_inert_batch(batch)
            or self._inert_ci_batch(batch)
        ):
            return None
        return batch

    def _emergency(self, now: float) -> None:
        """The tick's emergency step: settle cancel intents, end a dead emergency batch, build one for
        the oldest request when none is live, and report what an open one waits for."""
        self._settle_cancel_intents(now)
        self._prune_preserved()
        self._prune_evicted()
        batch = self._emergency_batch()
        if batch is not None:
            if batch["state"] == BATCH_OPEN and not self._members(batch["id"]):
                # A crash after allocation, or a probe that did not insert: the request is retried.
                self._end_emergency(batch, "empty-allocation", now, keep_request=True)
                batch = None
            elif not self._active_members(batch):
                self._end_emergency(batch, "emptied", now)
                batch = None
            elif batch["state"] == BATCH_OPEN and (self._mixed_inert_batch(batch) or self._inert_ci_batch(batch)):
                self._end_emergency(batch, "inert", now)
                batch = None
        if batch is None:
            batch = self._pick_emergency(now)
        if batch is None or batch["state"] != BATCH_OPEN:
            return
        head = self._head()
        if head is not None:
            if self.setting("emergency_evict"):
                self._evict(head, now, batch=batch)
            else:
                self._emergency_waits(batch["id"], f"head {head['id']}", now)

    def _emergency_waits(self, batch_id: int | None, why: str, now: float) -> None:
        self._reasons.append(f"emergency batch {batch_id} waits for {why} to land" if batch_id else why)
        key = f"{batch_id}:{why}"
        if self._state("emergency_waits") != key:
            self._set_state("emergency_waits", key)
            self.store.journal(now, "emergency-waits", batch_id, why=why)

    def _pick_emergency(self, now: float) -> dict[str, Any] | None:
        """Build the emergency batch for the oldest request, first in first out."""
        for row in self._flagged_inputs():
            if row["state"] == INPUT_RESERVED:
                return None  # its ordinary probe is running; it is waiting or inserted afterwards
            if isinstance(row["number"], SourceNumber) and (self.mixed is None or self.mixed.mode != "admit"):
                with self.store.transaction():
                    self._clear_emergency_request(row)
                    self.store.journal(now, "emergency-refused", number=row["number"], reason="forgejo-inert")
                continue
            if isinstance(row["number"], SourceNumber) and not self.setting("forgejo_placement_enabled"):
                continue  # held with every Forgejo input while placement is paused; the request is kept
            if row["state"] == INPUT_WAITING and self._label_blocked(row):
                continue  # held while a blocking label stays; the request is kept
            if row["state"] == INPUT_WAITING:
                return self._build_emergency(row, now)
            owner = self.store.row("batches", {"id": row["batch_id"]}) if row["batch_id"] is not None else None
            if owner is None or owner["state"] not in (BATCH_OPEN, BATCH_HEAD):
                continue
            if owner["state"] == BATCH_HEAD:
                others = [m for m in self._active_members(owner) if m["input_id"] != row["id"]]
                if others and self.setting("emergency_evict"):
                    # The PR leaves the running head for a batch of its own, which takes the head.
                    self._evict(owner, now, row=row)
                    return None
                # The running head carries the PR (alone, or eviction is off): it lands with the head.
                self._emergency_waits(None, f"emergency request #{row['number']} lands with head {owner['id']}", now)
                return None
            return self._emergency_singleton(row, owner, now)
        return None

    def _build_emergency(self, row: dict[str, Any], now: float) -> dict[str, Any] | None:
        """Allocate the emergency batch on `main` for a waiting input and probe the input into it.

        `_probe` runs unlocked; no transaction is held across it. Its refusals refuse the request
        (`conflicts-with-main` and `missing-source` also cancel the input, as for any probe); a
        transient failure (`probe-error`, `batch-moved`, `admission-pending`) ends the allocation and
        keeps the request for the next pass."""
        canonical = self._canonical
        if canonical is None:
            return None
        with self.store.transaction():
            batch = self._create_open_batch(canonical, now)
            self.store.update("batches", {"id": batch["id"]}, urgent=URGENT_EMERGENCY)
            evidence = loads(row["evidence"], {})
            evidence["emergency"]["batch_id"] = batch["id"]
            self.store.update("inputs", {"id": row["id"]}, evidence=dumps(evidence))
            self.store.journal(now, "emergency-batch", batch["id"], number=row["number"], state=row["state"])
        batch = self.store.row("batches", {"id": batch["id"]}) or batch
        inserted = self._probe(self.store.row("inputs", {"id": row["id"]}) or row, batch, now)
        current = self.store.row("batches", {"id": batch["id"]})
        if current is None or current["state"] not in (BATCH_OPEN, BATCH_HEAD):
            return None  # cancelled while the probe ran
        if inserted:
            return current
        fresh = self.store.row("inputs", {"id": row["id"]}) or row
        refused = None
        if fresh["state"] == INPUT_CANCELLED:
            refused = fresh["reason"]
        elif fresh["state"] == INPUT_WAITING and (
            fresh["last_probe"] == MISSING_SOURCE_IN_FLIGHT or fresh["last_probe"].startswith("source-ci-scope:")
        ):
            refused = fresh["last_probe"].split(":", 1)[0]
        if refused is not None:
            self._end_emergency(current, "empty-allocation", now, refused=refused)
        else:
            self._end_emergency(current, "empty-allocation", now, keep_request=True)
        return None

    def _emergency_singleton(self, row: dict[str, Any], owner: dict[str, Any], now: float) -> dict[str, Any] | None:
        """Build the emergency batch for a PR inserted in open batch `owner` (the prospective singleton).

        The PR's accepted head is replayed onto `main` without the lock (`_singleton_chain`); nothing is
        written before that succeeds, so a refusal leaves the input in `owner`. Then one transaction
        allocates E with its member and moves the input's ownership to it (`_insert_singleton`)."""
        built = self._singleton_chain(row, owner, now)
        if built is None or self._singleton_current(row, owner, built) is None:
            return None  # refused, or something moved while the replay ran; the next pass tries again
        with self.store.transaction():
            batch = self._insert_singleton(row, owner, built, now)
        return self.store.row("batches", {"id": batch["id"]})

    def _singleton_chain(self, row: dict[str, Any], owner: dict[str, Any], now: float) -> dict[str, Any] | None:
        """Replay the accepted head of a PR inserted in `owner` onto `main` without the lock, with the
        checks a probe makes (conflict, new import gaps, source CI scope). A refusal clears the request
        and leaves the input in `owner`; nothing else is written."""
        slot = self.store.row("members", {"batch_id": owner["id"], "input_id": row["id"], "removed": 0})
        canonical = self._canonical
        if slot is None or slot["withdrawn"] or canonical is None:
            return None
        member = self._chain_member(slot)
        chain_id = f"emergency-{row['id']}-{secrets.token_hex(4)}"
        failure: chain.ChainError | None = None
        result: chain.ChainResult | None = None
        gaps: list[tuple[str, str]] = []
        scope_supported = True
        try:
            with self._lock.unlocked():
                if not self._fetch_head(row["number"], row["head_sha"]):
                    raise chain.ChainError(f"accepted head {row['head_sha']} is not available")
                result = chain.build(self.clone, canonical, [member], chain_id=chain_id)
                gaps, _, scope_supported = self._replay_preflight(canonical, canonical, result)
        except chain.ChainError as error:
            failure = error
        finally:
            chain.delete_chain_ref(self.clone, chain_id)
        if failure is not None:
            self.store.journal(now, "emergency-build-error", owner["id"], number=row["number"], error=str(failure))
            self._reasons.append(f"emergency batch for #{row['number']} could not be built: {failure}")
            return None
        assert result is not None
        refused = (
            "conflicts-with-main"
            if result.status != chain.CLEAN
            else "missing-source"
            if gaps
            else "source-ci-scope"
            if not scope_supported and self._source_ci_required(row["number"], None)
            else None
        )
        if refused is not None:
            fresh = self.store.row("inputs", {"id": row["id"]})
            if fresh is not None and self._emergency_requested(fresh):
                with self.store.transaction():
                    self._clear_emergency_request(fresh)
                    self.store.journal(
                        now, "emergency-refused", owner["id"], number=row["number"], reason=refused, kept_in=owner["id"]
                    )
            return None
        return {"slot": slot, "member": member, "result": result, "canonical": canonical}

    def _singleton_current(
        self, row: dict[str, Any], owner: dict[str, Any], built: dict[str, Any]
    ) -> dict[str, Any] | None:
        """The input, reloaded, when the prospective singleton `built` still applies: the request is
        live, the input still `inserted` in `owner` at its accepted head through a live slot, `owner`
        in the state it was built from, `main` unmoved and no emergency batch live; else None."""
        fresh = self.store.row("inputs", {"id": row["id"]})
        current = self.store.row("batches", {"id": owner["id"]})
        slot = self.store.row("members", {"id": built["slot"]["id"]})
        if (
            fresh is None
            or not self._emergency_requested(fresh)
            or fresh["state"] != INPUT_INSERTED
            or fresh["batch_id"] != owner["id"]
            or fresh["head_sha"] != row["head_sha"]
            or current is None
            or current["state"] != owner["state"]
            or slot is None
            or slot["removed"]
            or slot["withdrawn"]
            or self._canonical != built["canonical"]
            or self._emergency_batch() is not None
        ):
            return None
        return fresh

    def _insert_singleton(
        self, row: dict[str, Any], owner: dict[str, Any], built: dict[str, Any], now: float
    ) -> dict[str, Any]:
        """Allocate the emergency batch on `main` with the prospective singleton `built` as its member,
        and move the input's ownership to it, recording the displaced slot in
        `evidence.emergency.displaced`. Inside the caller's transaction, after `_singleton_current`."""
        fresh = self.store.row("inputs", {"id": row["id"]}) or row
        result, member, slot = built["result"], built["member"], built["slot"]
        record = result.commits[0]
        batch = self._create_open_batch(built["canonical"], now)
        self.store.insert(
            "members",
            batch_id=batch["id"],
            input_id=row["id"],
            source_pr=member.source_pr,
            number=row["number"],
            accepted_head=row["head_sha"],
            position=0,
            commit_sha=record.sha,
            provenance="clean",
            added_at=now,
            no_repair=row["no_repair"],
        )
        self.store.update("batches", {"id": batch["id"]}, version=1, tip=result.tip, urgent=URGENT_EMERGENCY)
        self._record_tip(batch["id"], 1, result.tip, now)
        self._retain(self.batch_ref(batch["id"], 1), result.tip)
        evidence = loads(fresh["evidence"], {})
        evidence["emergency"].update(batch_id=batch["id"], displaced={"batch_id": owner["id"], "member_id": slot["id"]})
        self.store.update("inputs", {"id": row["id"]}, batch_id=batch["id"], evidence=dumps(evidence))
        self.store.journal(
            now,
            "emergency-batch",
            batch["id"],
            number=row["number"],
            state=INPUT_INSERTED,
            displaced_from=owner["id"],
        )
        self.store.journal(
            now, "inserted", batch["id"], number=row["number"], version=1, commit=record.sha, empty=record.empty
        )
        return self.store.row("batches", {"id": batch["id"]}) or batch

    # Eviction (`emergency_evict`): the emergency batch takes the head from a running ordinary head.

    def _evict(
        self,
        head: dict[str, Any],
        now: float,
        *,
        batch: dict[str, Any] | None = None,
        row: dict[str, Any] | None = None,
    ) -> bool:
        """Make the emergency batch the head in place of the running ordinary head `head`, unless the
        head is about to land or publishing its final oleans (`_protection`); True when it did.

        `batch` is the open emergency batch E; with `row` instead, the PR is a member of `head` and E is
        the prospective singleton built from it, allocated only in the eviction transaction. In order:
        the stored checks; the prospective singleton's replay; a fresh strict observation of the head's
        attempt; revalidation of everything read; then one transaction that moves the input's
        ownership (prospective singleton), reopens the head with its chain, version and retained tasks
        (`emergency_evicted:<head>`), supersedes its pending attempt with a durable cancel intent
        (unless it is under a promotion-refusal hold, which it returns to) and makes E the head
        (`_make_head`). If a check fails nothing is written and E keeps waiting. What no transaction
        prevents: the run can start publishing between the observation and the cancel."""
        target = batch["id"] if batch is not None else None
        prefix = f"emergency request #{row['number']} waits for " if row is not None else ""

        def wait(why: str) -> bool:
            self._emergency_waits(target, f"{prefix}head {head['id']} ({why})", now)
            return False

        barrier = (
            self._seal_blocked(batch)
            if batch is not None
            else ("inference hold: not sealing", False)
            if self._state("inference_hold")
            else ((self._external_head_blocks_sealing(), False) if self._external_head_blocks_sealing() else None)
        )
        if barrier is not None:
            return wait(barrier[0])
        attempt = self._current_attempt(head)
        why = self._protection(head, attempt, observed=False)
        if why is not None:
            return wait(why)
        held = attempt is not None and self.store.get_setting(f"source-ci:promotion-hold:{attempt['id']}") is not None
        observe = attempt is not None and not held and self._needs_eviction_observation(attempt)
        if observe:
            assert attempt is not None
            # A head found protected by an observation within the polling interval is not read again
            # each pass: protection is decided on fresh data, and so is every eviction.
            recent = attempt["last_observed_at"] is not None and now - attempt["last_observed_at"] < int(
                self.setting("polling_interval_seconds")
            )
            why = self._protection(head, attempt, observed=True) if recent else None
            if why is not None:
                return wait(why)
        built = None
        if row is not None:
            built = self._singleton_chain(row, head, now)
            if built is None:
                return False
        observed = attempt
        if observe:
            assert attempt is not None
            try:
                observed = self._eviction_observation(head, attempt, now)
            except Exception as error:  # noqa: BLE001 - no eviction this pass
                self.log.warning("eviction of head %s: observation failed: %s", head["id"], error)
                return wait("its CI run could not be read")
            why = self._protection(head, observed, observed=True)
            if why is not None:
                return wait(why)
        current = self._head()
        current_attempt = self._current_attempt(current) if current is not None else None
        valid = (
            current is not None
            and all(current[key] == head[key] for key in ("id", "version", "tip"))
            and (current_attempt or {}).get("id") == (attempt or {}).get("id")
            and not [c for c in self._active_claims(head["id"]) if c["task_id"] is None]
            and self._protection(current, current_attempt, observed=observe) is None
        )
        if valid and built is not None:
            valid = (
                self._singleton_current(row, head, built) is not None
                and not self._state("inference_hold")
                and not self._external_head_blocks_sealing()
            )
        elif valid:
            fresh = self.store.row("batches", {"id": target})
            valid = (
                fresh is not None
                and fresh["state"] == BATCH_OPEN
                and bool(self._active_members(fresh))
                and self._seal_blocked(fresh) is None
            )
        if not valid:
            blocked = self._seal_blocked(batch) if batch is not None else None
            return wait(blocked[0] if blocked else "it moved")
        assert current is not None
        with self.store.transaction():
            if built is not None:
                assert row is not None
                batch = self._insert_singleton(row, current, built, now)
            assert batch is not None
            self.store.update(
                "batches",
                {"id": current["id"]},
                state=BATCH_OPEN,
                expected_parent=None,
                prepared_version=current["version"],
            )
            self._write_cancel_intents(
                current,
                f"evicted by emergency batch {batch['id']}",
                now,
                keep=current_attempt["id"] if held and current_attempt is not None else None,
            )
            tasks = self.store.rows(
                "tasks", {"batch_id": current["id"], "state": TASK_OPEN, "batch_version": current["version"]}
            )
            self._set_state(
                f"emergency_evicted:{current['id']}",
                {
                    "version": current["version"],
                    "tip": current["tip"],
                    "by": batch["id"],
                    "attempt_id": (current_attempt or {}).get("id"),
                    "task_ids": [task["id"] for task in tasks],
                    "at": now,
                },
            )
            self._make_head(self.store.row("batches", {"id": batch["id"]}) or batch, now)
            self.store.journal(
                now,
                "evicted",
                current["id"],
                version=current["version"],
                tip=current["tip"],
                attempt_id=(current_attempt or {}).get("id"),
                run_id=(observed or {}).get("run_id"),
                phases=loads((observed or {}).get("phases"), []),
                by=batch["id"],
                why=self._evictable(current_attempt, held),
                held=held,
                tasks=[task["id"] for task in tasks],
            )
            self._set_state("emergency_waits", None)
        self._count("heads_evicted")
        self._settle_cancel_intents(now)
        self._wake.set()
        return True

    @staticmethod
    def _needs_eviction_observation(attempt: dict[str, Any]) -> bool:
        """A pending attempt whose run may exist: only a fresh observation can say it is not publishing."""
        return attempt["outcome"] == OUTCOME_PENDING and attempt["dispatch_state"] not in ("prepared", "rejected")

    def _protection(self, head: dict[str, Any], attempt: dict[str, Any] | None, *, observed: bool) -> str | None:
        """Why the head may not be evicted, or None. Decided in order on the head's current attempt:
        a promotion-refusal hold is never protection; a pending `promote` entry (P1) or a pass (P2) is.
        With `observed` (the attempt was just read with `_eviction_observation`), a pending attempt is
        also protected when its run completed `success` and its verdicts are being bound (P3), or it is
        publishing its final oleans (P4, `publishing`). Everything else is evictable."""
        if self.store.query(
            "SELECT 1 FROM outbox WHERE kind='promote' AND state='pending' AND key LIKE ? LIMIT 1",
            (f"promote:{head['id']}:%",),
        ):
            hold = attempt is not None and self.store.get_setting(f"source-ci:promotion-hold:{attempt['id']}")
            if not hold:
                return "its promotion is pending"
        if attempt is None or self.store.get_setting(f"source-ci:promotion-hold:{attempt['id']}") is not None:
            return None
        if attempt["outcome"] == OUTCOME_PASS:
            return "it passed and is about to land"
        if not observed or attempt["outcome"] != OUTCOME_PENDING:
            return None
        if attempt["run_status"] == "completed" and attempt["run_conclusion"] == "success":
            return "its run succeeded and its verdict is being bound"
        if attempt["run_status"] != "completed" and publishing(loads(attempt["phases"], [])):
            return "it is publishing its final oleans"
        return None

    def _evictable(self, attempt: dict[str, Any] | None, held: bool) -> str:
        """Why an evicted head was not protected, for the `evicted` journal entry."""
        if attempt is None:
            return "no CI attempt"
        if held:
            return "promotion-refusal hold"
        if attempt["outcome"] != OUTCOME_PENDING:
            return f"CI {attempt['outcome']}"
        if attempt["dispatch_state"] in ("prepared", "rejected"):
            return f"dispatch {attempt['dispatch_state']}"
        return f"run {attempt['run_status'] or 'not found'}"

    def _eviction_observation(self, head: dict[str, Any], attempt: dict[str, Any], now: float) -> dict[str, Any]:
        """The head's pending attempt after a fresh strict observation (`_observe_attempt(strict=True)`),
        reloaded; raises when it cannot be read. A source-candidate run in progress also has its phases
        read afresh, past `SOURCE_PHASES_INTERVAL_SECONDS`, and raises when that read fails."""
        self.store.update("ci_attempts", {"id": attempt["id"]}, needs_observation=1)
        row = self.store.row("ci_attempts", {"id": attempt["id"]}) or attempt
        source = self._candidate_coordinator(attempt_id=attempt["id"]) is not None
        if source:
            for key in [key for key in self._source_phases_read if key[0] == attempt["id"]]:
                del self._source_phases_read[key]
        self._observe_attempt(head, row, now, strict=True)
        row = self.store.row("ci_attempts", {"id": attempt["id"]}) or row
        if source and row["outcome"] == OUTCOME_PENDING and row["run_status"] == "in_progress":
            key = (row["id"], row["run_id"], row["run_attempt"])
            if key not in self._source_phases_read or key in self._source_phases_unavailable:
                raise ControllerError("phases-unavailable", f"source CI phases of run {row['run_id']} are unavailable")
        return row

    def _eviction(self, batch: dict[str, Any]) -> dict[str, Any] | None:
        """The eviction marker of an open batch the emergency batch took the head from, else None."""
        if batch["state"] != BATCH_OPEN:
            return None
        return self._state(f"emergency_evicted:{batch['id']}")

    def _evicted_task_current(self, task: dict[str, Any], batch: dict[str, Any]) -> bool:
        """A task the evicted batch had open as the head, still current while the batch waits, open
        and sealed at the version it was evicted at: its owner may submit it and it stays claimable."""
        marker = self._eviction(batch)
        return bool(
            marker
            and batch["sealed"]
            and task["state"] == TASK_OPEN
            and task["id"] in marker["task_ids"]
            and batch["version"] == marker["version"] == task["batch_version"]
        )

    def _open_rebase_current(self, task: dict[str, Any], batch: dict[str, Any]) -> bool:
        """An open batch's own `rebase_conflict` task onto `main` (`_onto_canonical`), not an evicted
        head's retained one: current, claimable and adoptable while the batch is open at its version."""
        return (
            task["kind"] == TASK_REBASE_CONFLICT
            and task["state"] == TASK_OPEN
            and batch["state"] == BATCH_OPEN
            and batch["version"] == task["batch_version"]
            and self._eviction(batch) is None
        )

    def _open_rebase_task(self, batch: dict[str, Any]) -> dict[str, Any] | None:
        """The open batch's own open `rebase_conflict` task onto `main`, if any."""
        if batch["state"] != BATCH_OPEN or self._eviction(batch) is not None:
            return None
        return self.store.row("tasks", {"batch_id": batch["id"], "state": TASK_OPEN, "kind": TASK_REBASE_CONFLICT})

    def _retained_tasks(self, batch: dict[str, Any]) -> list[dict[str, Any]]:
        """The evicted batch's open retained tasks: while there are any, nothing else is created for it."""
        if self._eviction(batch) is None:
            return []
        return [
            task
            for task in self.store.rows("tasks", {"batch_id": batch["id"], "state": TASK_OPEN})
            if self._evicted_task_current(task, batch)
        ]

    def _prune_evicted(self) -> None:
        """Drop the eviction markers of batches that are no longer open (head again, landed, removed)."""
        for key in self.store.query("SELECT key FROM settings WHERE key LIKE 'state:emergency_evicted:%'"):
            batch = self.store.row("batches", {"id": int(key["key"].rsplit(":", 1)[1])})
            if batch is None or batch["state"] != BATCH_OPEN:
                self.store.execute("DELETE FROM settings WHERE key=?", (key["key"],))

    def _end_emergency(
        self,
        batch: dict[str, Any],
        cause: str,
        now: float,
        *,
        keep_request: bool = False,
        refused: str | None = None,
        actor: str | None = None,
    ) -> None:
        """End emergency batch `batch` without landing it; the one request-scoped cleanup path.

        In one transaction: its members leave it (`_return_members`, so `_emergency_exit`: back to a
        live displaced slot, else waiting at the original age); an input its own probe holds reserved
        waits again; the request is cleared unless `keep_request` (a transient `empty-allocation`, retried
        next pass); its open tasks are superseded and its claims released; pre-rebases onto its tips are
        discarded; it is removed; and a durable cancel intent is written for every unsettled attempt of
        it at any version (`_write_cancel_intents`). The intents are then settled at once."""
        input_id = self._emergency_input(batch)
        number = None
        with self.store.transaction():
            for member in self._members(batch["id"]):
                if not member["withdrawn"]:
                    self._return_members(batch, [member], f"emergency-{cause}", now)
            row = self.store.row("inputs", {"id": input_id}) if input_id is not None else None
            if row is not None:
                number = row["number"]
                if row["state"] == INPUT_RESERVED and row["batch_id"] == batch["id"]:
                    # Its own probe runs unlocked: the input waits again and the returning probe finds
                    # it no longer reserved.
                    self.store.update("inputs", {"id": row["id"]}, state=INPUT_WAITING, batch_id=None)
                    row = self.store.row("inputs", {"id": row["id"]}) or row
                if not keep_request and self._emergency_requested(row):
                    self._clear_emergency_request(row)
            for stale in self.store.rows("tasks", {"batch_id": batch["id"], "state": TASK_OPEN}):
                self.store.update(
                    "tasks", {"id": stale["id"]}, state=TASK_SUPERSEDED, resolved_at=now, resolution="emergency-ended"
                )
            for claim in self._active_claims(batch["id"]):
                self._release_claim(claim, "superseded", now)
            self._set_pre_rebase(batch, "", now)
            current = self.store.row("batches", {"id": batch["id"]}) or batch
            if current["version"] > 0:
                # A version-0 allocation's only tip is `main`, which successor work may rightly be onto.
                self._discard_pre_rebase("emergency-ended", now, current)
            self.store.update("batches", {"id": batch["id"]}, state=BATCH_REMOVED)
            self.store.journal(now, "batch-removed", batch["id"], reason=f"emergency-{cause}")
            self.store.journal(
                now,
                "emergency-ended",
                batch["id"],
                number=number,
                cause=cause,
                retried=keep_request,
                **({"refused": refused} if refused else {}),
                **({"actor": actor} if actor else {}),
            )
            if refused:
                self.store.journal(now, "emergency-refused", batch["id"], number=number, reason=refused)
            self._clear_admission_state(batch["id"])
            self._write_cancel_intents(batch, f"emergency batch ended ({cause})", now)
            self._set_state("emergency_waits", None)
        self._settle_cancel_intents(now)
        self._wake.set()

    # Durable run cancellation: `cancel_intent:<attempt>` (under `state:`) until the run is settled.

    def _cancel_settled(self, attempt: dict[str, Any], now: float, *, looked_up: bool) -> bool:
        """Nothing is left to cancel for the attempt: its cancel was accepted, its run completed, it never
        had a run (`prepared`, `rejected`), or (after a lookup found none) an accepted dispatch is older
        than `dispatch_timeout_seconds`. A `sending`/`uncertain` dispatch is never settled by age."""
        if attempt["cancel_result"] in ("cancelled", "already-completed") or attempt["run_status"] == "completed":
            return True
        if attempt["dispatch_state"] in ("prepared", "rejected"):
            return True
        return bool(
            looked_up
            and not attempt["run_id"]
            and attempt["dispatch_state"] in ("accepted", "legacy")
            and now - (attempt["sent_at"] or attempt["dispatched_at"]) >= int(self.setting("dispatch_timeout_seconds"))
        )

    def _write_cancel_intent(
        self, attempt: dict[str, Any], reason: str, now: float, *, failed: BaseException | None = None
    ) -> None:
        """Record a cancel intent for `attempt` unless one is open; `_settle_cancel_intents` pursues it.
        `failed`: the error of a lookup or cancel of it that just failed, so the first retry waits out
        a backoff (`_defer_cancel_intent`)."""
        key = f"cancel_intent:{attempt['id']}"
        if self._state(key) is None:
            self._set_state(key, {"batch_id": attempt["batch_id"], "reason": reason, "at": now})
            if failed is not None:
                self._defer_cancel_intent(attempt["id"], self._state(key), now, failed)

    @staticmethod
    def _error_retry_at(error: BaseException | None) -> float:
        """GitHub's own deadline carried by `error` or the error it was raised from, else 0."""
        seen = 0
        while error is not None and seen < 8:
            retry_at = getattr(error, "retry_at", None)
            if isinstance(retry_at, int | float) and not isinstance(retry_at, bool):
                return float(retry_at)
            error, seen = error.__cause__ or error.__context__, seen + 1
        return 0.0

    def _defer_cancel_intent(
        self, attempt_id: int, intent: dict[str, Any], now: float, error: BaseException | None = None
    ) -> None:
        """Back off a cancel intent whose lookup or cancel failed, as the outbox does (`_outbox_failed`).

        The backoff is measured from the clock after the failure (a slow request must not leave the
        retry already overdue), and never ends before a rate-limit deadline GitHub gave `error`."""
        tries = int(intent.get("tries", 0)) + 1
        backoff = min(
            int(self.setting("outbox_retry_seconds")) * (2 ** min(20, tries - 1)),
            int(self.setting("outbox_retry_max_seconds")),
        )
        retry_at = max(max(now, self.clock()) + backoff, self._error_retry_at(error))
        self._set_state(f"cancel_intent:{attempt_id}", dict(intent, tries=tries, retry_at=retry_at))

    def _write_cancel_intents(self, batch: dict[str, Any], reason: str, now: float, *, keep: int | None = None) -> None:
        """Record a cancel intent for every unsettled attempt of `batch` other than `keep`, at any version;
        a pending one becomes `superseded`. Called inside the caller's transaction."""
        for attempt in self.store.rows("ci_attempts", {"batch_id": batch["id"]}, order="id"):
            if attempt["id"] == keep or self._cancel_settled(attempt, now, looked_up=False):
                continue
            self._write_cancel_intent(attempt, reason, now)
            if attempt["outcome"] == OUTCOME_PENDING:
                self.store.update(
                    "ci_attempts",
                    {"id": attempt["id"], "outcome": OUTCOME_PENDING},
                    outcome=OUTCOME_SUPERSEDED,
                    outcome_detail=f"ci-superseded: {reason}",
                )

    def _cancel_intents(self) -> dict[int, dict[str, Any]]:
        rows = self.store.query("SELECT key, value FROM settings WHERE key LIKE 'state:cancel_intent:%' ORDER BY key")
        return {int(row["key"].rsplit(":", 1)[1]): loads(row["value"], {}) for row in rows}

    def _drop_cancel_intent(self, attempt_id: int) -> None:
        self.store.execute("DELETE FROM settings WHERE key=?", (f"state:cancel_intent:{attempt_id}",))

    def _settle_cancel_intents(self, now: float) -> None:
        """Pursue every cancel intent until its run is positively settled.

        An attempt without a run whose dispatch may have been sent has its run looked up first, whatever
        its outcome; a live run is cancelled. The intent is deleted only when the cancel was accepted,
        the run is observed completed, or the attempt provably has no run (`_cancel_settled`). A failed
        lookup or cancel keeps the intent and backs it off (`_defer_cancel_intent`); there is no cap.
        Intents block nothing."""
        for attempt_id, intent in self._cancel_intents().items():
            attempt = self.store.row("ci_attempts", {"id": attempt_id})
            if attempt is None or self._cancel_settled(attempt, now, looked_up=False):
                self._drop_cancel_intent(attempt_id)
                continue
            if intent.get("retry_at", 0) > now:
                continue
            try:
                candidate_ci = self._candidate_coordinator(attempt_id=attempt_id)
            except ControllerError:
                continue  # retained for its disabled source's coordinator
            if candidate_ci is None and self.store.row("ci_requests", {"attempt_id": attempt_id}) is not None:
                continue
            cancel = (
                candidate_ci.cancel_run if candidate_ci is not None else getattr(self.execution, "cancel_run", None)
            )
            if cancel is None:
                continue
            if not attempt["run_id"]:
                if attempt["dispatch_state"] not in {"sending", "uncertain", "accepted", "legacy"}:
                    continue
                if any(
                    later["id"] > attempt_id
                    and later["head_sha"] == attempt["head_sha"]
                    and later["base_sha"] == attempt["base_sha"]
                    and later["dispatch_state"] in {"sending", "uncertain", "accepted", "legacy"}
                    for later in self.store.rows("ci_attempts", {"batch_id": attempt["batch_id"]})
                ):
                    # A later dispatch of the same candidate was sent (a head that returned after an
                    # eviction): a lookup now finds that newer run, which this intent must never take,
                    # and pr-build.yml's concurrency group cancels the earlier run when the later starts.
                    self._drop_cancel_intent(attempt_id)
                    continue
                try:
                    if candidate_ci is not None:
                        with self._lock.unlocked():
                            run = candidate_ci.observe(attempt_id).run
                    else:
                        run = self.execution.find_candidate_run(
                            self.repository,
                            batch_id=str(attempt["batch_id"]),
                            head_sha=attempt["head_sha"],
                            base_sha=attempt["base_sha"],
                            since=attempt["sent_at"] or attempt["dispatched_at"],
                        )
                    empty = None
                except NoOwner as error:
                    # A complete listing found no run: a successful empty lookup, settled by the
                    # rules below. It grants no ownership, so nothing is cancelled for it.
                    run, empty = None, error
                except Exception as error:  # noqa: BLE001 - retried next pass
                    self.log.warning("cancel intent %s: run lookup failed: %s", attempt_id, error)
                    self._note_reason(f"cancel intent for CI attempt {attempt_id}: run lookup failed")
                    self._defer_cancel_intent(attempt_id, intent, now, error)
                    continue
                others = {
                    str(a["run_id"])
                    for a in self.store.rows("ci_attempts", {"batch_id": attempt["batch_id"]})
                    if a["id"] != attempt_id and a["run_id"]
                }
                if run is not None and str(run.run_id) not in others:
                    self.store.update("ci_attempts", {"id": attempt_id}, run_id=run.run_id, run_status=run.status)
                    attempt = self.store.row("ci_attempts", {"id": attempt_id}) or attempt
                elif self._cancel_settled(attempt, now, looked_up=True):
                    self._drop_cancel_intent(attempt_id)
                    continue
                else:
                    if empty is not None:
                        # A `sending`/`uncertain` dispatch, or one still inside its timeout, is kept;
                        # its next complete discovery waits out a backoff.
                        self._defer_cancel_intent(attempt_id, intent, now, empty)
                    continue
            if attempt["run_status"] == "completed":
                self._drop_cancel_intent(attempt_id)
                continue
            run_id = str(attempt["run_id"])
            failure = None
            try:
                if candidate_ci is not None:
                    with self._lock.unlocked():
                        result = cancel(run_id)
                else:
                    result = cancel(self.repository, run_id)
            except Exception as error:  # noqa: BLE001 - retried next pass
                result, failure = f"failed: {str(error)[:200]}", error
            with self.store.transaction():
                self.store.update(
                    "ci_attempts",
                    {"id": attempt_id},
                    cancelled_at=attempt["cancelled_at"] or now,
                    cancel_reason=intent.get("reason", ""),
                    cancel_result=result,
                )
                self.store.journal(
                    now,
                    "ci-cancelled",
                    attempt["batch_id"],
                    attempt_id=attempt_id,
                    run_id=run_id,
                    reason=intent.get("reason", ""),
                    result=result,
                    intent=True,
                )
                if result in ("cancelled", "already-completed"):
                    self._count("ci_runs_cancelled")
                    self._drop_cancel_intent(attempt_id)
                else:
                    self._defer_cancel_intent(attempt_id, intent, now, failure)
            if result not in ("cancelled", "already-completed"):
                self.log.warning("cancel intent %s: cancelling CI run %s %s", attempt_id, run_id, result)

    # Claimed successor work across the emergency batch becoming the head.

    def _preserve_successor_tasks(self, now: float) -> None:
        """Record `{task: onto}` for every claimed `pre_rebase` task and claimed pre-rebased `prepare`
        task of an ordinary open batch, as the emergency batch becomes the head: their intended parent
        (the previous head's tip) is no longer `_pre_rebase_target`, but their owner may still submit
        onto it (`_preserved_onto`); the next `_pre_rebase` then carries the result onto the new head."""
        preserved = {}
        for batch in self._staging_batches():
            for claim in self._active_claims(batch["id"]):
                task = self.store.row("tasks", {"id": claim["task_id"]}) if claim["task_id"] is not None else None
                if task is not None and task["state"] == TASK_OPEN:
                    if task["kind"] == TASK_PRE_REBASE or self._pre_rebased_prepare(task):
                        preserved[str(task["id"])] = task["onto"]
        self._set_state("emergency_preserved", preserved)
        if preserved:
            self.store.journal(now, "emergency-preserved", tasks=sorted(int(task_id) for task_id in preserved))

    def _preserved_onto(self, task: dict[str, Any]) -> str | None:
        """The parent a preserved task may still submit onto, while it is open and claimed."""
        onto = (self._state("emergency_preserved") or {}).get(str(task["id"]))
        if onto is None or task["state"] != TASK_OPEN or not self._active_claims(task["batch_id"], task_id=task["id"]):
            return None
        return onto

    def _preserved_tasks(self, batch: dict[str, Any]) -> list[dict[str, Any]]:
        """The batch's preserved tasks still claimed: its stored pre-rebase is not rewritten under them."""
        if not self._state("emergency_preserved"):
            return []
        return [
            task
            for task in self.store.rows("tasks", {"batch_id": batch["id"], "state": TASK_OPEN})
            if self._preserved_onto(task) is not None
        ]

    def _prune_preserved(self) -> None:
        preserved = self._state("emergency_preserved") or {}
        kept = {
            task_id: onto
            for task_id, onto in preserved.items()
            if (task := self.store.row("tasks", {"id": int(task_id)})) is not None and self._preserved_onto(task)
        }
        if kept != preserved:
            self._set_state("emergency_preserved", kept)

    def _emergency_status(self) -> dict[str, Any]:
        """`/status`'s `emergency`: the live emergency batch, the queued requests and the open cancel intents."""

        def request(row: dict[str, Any]) -> dict[str, Any]:
            record = loads(row["evidence"], {}).get("emergency") or {}
            return {
                "number": int(row["number"]),
                **self._public_pull_identity(row["number"]),
                "state": row["state"],
                **{key: record.get(key) for key in ("actor", "requested_at", "surface", "reason")},
            }

        batch = self._emergency_batch()
        active = None
        if batch is not None:
            input_id = self._emergency_input(batch)
            row = self.store.row("inputs", {"id": input_id}) if input_id is not None else None
            head = self._head()
            waiting_for = None
            if batch["state"] == BATCH_OPEN:
                waiting_for = (
                    f"head {head['id']} to land"
                    if head is not None
                    else "a member"
                    if not self._active_members(batch)
                    else "sealing"
                )
            active = {
                "batch_id": batch["id"],
                "stage": "head" if batch["state"] == BATCH_HEAD else "waiting",
                "waiting_for": waiting_for,
                **(request(row) if row is not None else {}),
            }
        served = {self._emergency_input(batch)} if batch is not None else set()
        return {
            "enabled": bool(self.setting("emergency_enabled")),
            "evict": bool(self.setting("emergency_evict")),
            "batch": active,
            "queued": [request(row) for row in self._flagged_inputs() if row["id"] not in served],
            "cancel_intents": [
                {"attempt_id": attempt_id, **intent} for attempt_id, intent in self._cancel_intents().items()
            ],
        }

    # --- sealing -----------------------------------------------------------------------------------

    def _seal(self, now: float) -> None:
        head = self._head()
        if head is not None:
            lane = self._lane_ready()
            if lane is not None:
                self._reasons.append(f"control-file lane batch {lane['id']} waits for head {head['id']} to land")
            self._seal_successor(now)
            return
        # The emergency batch is staged ahead of every open batch (`_emergency_ready`), and the
        # control-file lane batch next (`_lane_ready`): it never waits behind an ordinary batch.
        batch = self._emergency_ready() or self._lane_ready() or self._open_batch()
        if batch is None or not self._members(batch["id"]):
            return
        blocked = self._seal_blocked(batch)
        if (blocked is None or blocked[1]) and self._seal_clean_prefix(batch, now):
            self._seal_successor(now)
            return
        if blocked is not None:
            reason, claimed = blocked
            if claimed:
                self._seal_successor(now)
            self._reasons.append(reason)
            return
        self._make_head(batch, now)
        self._seal_successor(now)

    def _seal_blocked(self, batch: dict[str, Any]) -> tuple[str, bool] | None:
        """Why `batch` cannot become the head now, and whether that is a claim on it; None when it can."""
        if self._state("inference_hold"):
            return "inference hold: not sealing", False
        blocked = self._external_head_blocks_sealing()
        if blocked:
            return blocked, False
        if self._active_claims(batch["id"]):
            # Keep the batch open while its owner prepares or pre-rebases it. Adoption uses the
            # ordinary open-batch validation, and the next head keeps the completed work.
            return f"open batch {batch['id']} is claimed; sealing waits for the release", True
        return None

    def _seal_clean_prefix(self, batch: dict[str, Any], now: float) -> bool:
        """Seal the members of `batch` that replay cleanly onto `main`, when its pre-rebase is a conflict.

        The slot is free, but resolving the conflict (a `pre_rebase` task, claimed or not) takes hours:
        rather than wait for it, or seal everything and hand the head a `rebase_conflict` task, the
        members before the conflict move to a new batch that becomes the head. The conflicting member
        and those after it stay in `batch`, now based on the last moved member's commit, so it keeps its
        place as the next batch and its members their approvals and age; a return to waiting would
        instead cancel a member that conflicts with `main`. The batch's `pre_rebase` tasks are
        superseded (resolution `clean-prefix`) and their claims released; the next pre-rebase onto the
        new head creates a fresh task. False (nothing changed) for the emergency or control-file lane
        batch, a claim that is not a `pre_rebase` task's, a conflict at the first member, or a replay error.
        """
        if (
            batch["pre_rebase_state"] != PRE_REBASE_CONFLICT
            or batch["urgent"] == URGENT_EMERGENCY
            or batch["lane"] == LANE_CONTROL
            or self._canonical is None
        ):
            return False
        for claim in self._active_claims(batch["id"]):
            task = self.store.row("tasks", {"id": claim["task_id"]}) if claim["task_id"] is not None else None
            if task is None or task["kind"] != TASK_PRE_REBASE:
                return False
        chain_id = f"clean-prefix-{batch['id']}-{secrets.token_hex(4)}"
        try:
            result = chain.replay(self.clone, batch["base"], batch["tip"], self._canonical, chain_id=chain_id)
        except chain.ChainError as error:
            self.store.journal(now, "pre-rebase-error", batch["id"], onto=self._canonical, error=str(error))
            return False
        finally:
            chain.delete_chain_ref(self.clone, chain_id)
        # The chain's own order decides the prefix (an adopted preparation may reorder commits).
        commits = chain.chain_commits(self.clone, batch["base"], batch["tip"])
        by_pr = {member["source_pr"]: member for member in self._members(batch["id"])}
        order = [commit.source_pr or commit.sha for commit in commits]
        if sorted(order) != sorted(by_pr) or [record.source_pr for record in result.commits] != order:
            return False
        cut = next((i for i, record in enumerate(result.commits) if record.sha is None), len(order))
        kept, left = [by_pr[pr] for pr in order[:cut]], [by_pr[pr] for pr in order[cut:]]
        if not kept:
            return False
        tip = commits[cut - 1].sha
        conflict = result.conflict
        with self.store.transaction():
            superseded = self._supersede_pre_rebase_tasks(batch["id"], "clean-prefix", now)
            self._set_pre_rebase(batch, "", now)
            if not left:
                head = batch  # the conflict is gone: the whole batch seals
            else:
                head_id = self.store.insert(
                    "batches",
                    repository=batch["repository"],
                    version=0,
                    base=batch["base"],
                    tip=tip,
                    sealed=0,
                    state=BATCH_OPEN,
                    created_at=now,
                    sealed_at=batch["sealed_at"],
                )
                self._record_tip(head_id, 0, tip, now)
                self._retain(self.batch_ref(head_id, 0), tip)
                for position, member in enumerate(kept):
                    self.store.update("members", {"id": member["id"]}, batch_id=head_id, position=position)
                    self.store.update("inputs", {"id": member["input_id"], "batch_id": batch["id"]}, batch_id=head_id)
                for position, member in enumerate(left):
                    # Insertion appends at `len(members)`: keep the positions dense, in chain order.
                    self.store.update("members", {"id": member["id"]}, position=position)
                version = batch["version"] + 1
                self.store.update("batches", {"id": batch["id"]}, version=version, base=tip, expected_parent=None)
                self._record_tip(batch["id"], version, batch["tip"], now)
                self._retain(self.batch_ref(batch["id"], version), batch["tip"])
                head = self.store.row("batches", {"id": head_id}) or {}
            self._count("clean_prefix_seals")
            self.store.journal(
                now,
                "clean-prefix-sealed",
                batch["id"],
                head_id=head["id"],
                onto=self._canonical,
                kept=[m["number"] for m in kept],
                left=[m["number"] for m in left],
                conflict=conflict.source_pr if conflict else None,
                tasks_superseded=superseded,
            )
            self._make_head(head, now)
        return True

    def _make_head(self, batch: dict[str, Any], now: float) -> None:
        """Seal `batch` as the head, which `_seal_blocked` allowed."""
        with self.store.transaction():
            # Unclaimed background offers give way to the new head's own work.
            self._supersede_prepare(batch["id"], "sealed", now)
            for stale in self.store.rows(
                "tasks", {"batch_id": batch["id"], "state": TASK_OPEN, "kind": TASK_PRE_REBASE}
            ):
                self.store.update(
                    "tasks", {"id": stale["id"]}, state=TASK_SUPERSEDED, resolved_at=now, resolution="sealed"
                )
            self.store.update(
                "batches",
                {"id": batch["id"]},
                sealed=1,
                state=BATCH_HEAD,
                sealed_at=batch["sealed_at"] if batch["sealed_at"] is not None else now,
            )
            self._count("heads_sealed")
            self.store.execute("DELETE FROM settings WHERE key=?", (f"state:emergency_evicted:{batch['id']}",))
            self.store.journal(
                now,
                "sealed",
                batch["id"],
                version=batch["version"],
                size=len(self._members(batch["id"])),
                urgent=batch["urgent"],
            )
            if batch["urgent"] == URGENT_EMERGENCY:
                self._preserve_successor_tasks(now)
                self.store.journal(now, "emergency-head", batch["id"], version=batch["version"])
            elif batch["lane"] == LANE_CONTROL and self._control_lane_active():
                # Like the emergency batch, the lane head goes ahead of the batch pre-rebased onto the
                # previous head: claimed successor work may still submit onto that tip.
                self._preserve_successor_tasks(now)
                self.store.journal(now, "control-lane-head", batch["id"], version=batch["version"])
            if self._canonical is not None and not self._unsealed_batches():
                self._create_open_batch(self._canonical, now)

    def _seal_successor(self, now: float) -> None:
        """Freeze oldest unsealed batches at the threshold until two active batches are sealed.

        The existing sealed flag fixes membership independently of `state`: a successor remains
        `open` for the background task protocol until promotion to `head`. An active background
        owner keeps its task and can adopt normally; insertions may never enter the sealed batch.
        """
        minimum = int(self.setting("sealed_successor_min_members"))
        if minimum == 0 or self._state("inference_hold") or self._external_head_blocks_sealing():
            return
        sealed = int(self._head() is not None) + sum(bool(batch["sealed"]) for batch in self._staging_batches())
        # Promotion may be waiting for the oldest preparation owner even after the previous
        # head landed. That still leaves capacity for a second sealed batch; do not wait for
        # the first to become head before freezing the next one's membership.
        while sealed < 2:
            batches = self._unsealed_batches()
            if not batches:
                return
            batch = batches[0]
            size = len(self._members(batch["id"]))
            if size < minimum or any(
                c["operation"] not in (*BACKGROUND_OPERATIONS, f"task:{TASK_REBASE_CONFLICT}")
                for c in self._active_claims(batch["id"])
            ):
                return
            with self.store.transaction():
                self.store.update("batches", {"id": batch["id"]}, sealed=1, sealed_at=now)
                self._count("successors_sealed")
                self.store.journal(
                    now,
                    "successor-sealed",
                    batch["id"],
                    version=batch["version"],
                    size=size,
                    sealed_before=sealed,
                    sealed_after=sealed + 1,
                )
            sealed += 1

    # --- head reconciliation -----------------------------------------------------------------------

    def _reconcile_head(self, now: float) -> None:
        head = self._head()
        if head is None:
            return
        if head["urgent"] == URGENT_EMERGENCY and not self._active_members(head):
            # Emptied between tick steps (a submission withdrew its member): never dispatched.
            self._end_emergency(head, "emptied", now)
            return
        canonical = self._canonical
        assert canonical is not None
        task_claims = [c for c in self._active_claims(head["id"]) if c["task_id"] is not None]
        if task_claims:
            if head["base"] != canonical:
                self._cancel_superseded_runs(head["id"], now, reason="canonical advanced; rebase waits for worker")
            # A speculative repair runs alongside its own candidate CI, whose observation
            # continues without permitting promotion or replacing the claimed task.
            active = self._open_task(head)
            if active is not None and self._prediction_task(active):
                attempt = self._current_attempt(head)
                if attempt is not None and attempt["outcome"] == OUTCOME_PENDING:
                    self._advance_attempt(head, attempt, now)
                    attempt = self.store.row("ci_attempts", {"id": attempt["id"]}) or attempt
                if attempt is not None and attempt["outcome"] == OUTCOME_PASS:
                    self._finish_prediction_pass(head, active, attempt, now)
                    return
                if attempt is not None:
                    self._update_prediction_verdict(head, active, attempt, now)
            self._reasons.append(f"head {head['id']} is claimed by {', '.join(c['worker'] for c in task_claims)}")
            return
        if head["base"] != canonical:
            if not self._rebase_head(head, canonical, now):
                self._cancel_superseded_runs(head["id"], now, reason="canonical advanced; rebase incomplete")
                return
            head = self._head()
            assert head is not None
        task = self._open_task(head)
        if task is not None:
            if self._prediction_task(task):
                attempt = self._current_attempt(head)
                if attempt is not None and attempt["outcome"] == OUTCOME_PENDING:
                    self._advance_attempt(head, attempt, now)
                    attempt = self.store.row("ci_attempts", {"id": attempt["id"]}) or attempt
                if attempt is not None and attempt["outcome"] == OUTCOME_PASS:
                    self._finish_prediction_pass(head, task, attempt, now)
                    return
                if attempt is not None:
                    self._update_prediction_verdict(head, task, attempt, now)
            self._reasons.append(
                f"head {head['id']} waits for an authorized worker to claim {task['kind']} task {task['id']}"
            )
            return
        self._ci(head, now)
        self._schedule_predicted_repair(head, now)

    def _rebase_head(self, head: dict[str, Any], canonical: str, now: float) -> bool:
        """Replay the head onto the canonical commit; True when it is now based there."""
        task = self._open_task(head)
        if task is not None and task["kind"] == "rebase_conflict" and task["onto"] == canonical:
            self._reasons.append(f"head {head['id']} waits for a worker to resolve rebase conflict task {task['id']}")
            return False
        if self._active_claims(head["id"]):
            self._reasons.append(f"head {head['id']} is claimed")
            return False
        if self._seal_from_pre_rebase(head, canonical, now):
            return True
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
                self._record_tip(head["id"], version, result.tip, now)
                self._set_pre_rebase(head, "", now)
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
        diagnostics = self._conflict_diagnostics(result, canonical)
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

    @staticmethod
    def _conflict_diagnostics(result: chain.ChainResult, onto: str) -> list[dict[str, Any]]:
        """A `rebase_conflict` task's diagnostics for a replay onto `onto` that conflicted."""
        conflict = result.conflict
        return [
            {
                "kind": "conflict",
                "source_pr": conflict.source_pr if conflict else None,
                # The input chain's conflicting commit, which the worker's own replay stops at.
                "commit": conflict.replayed if conflict else None,
                "paths": list(conflict.conflict_paths) if conflict else [],
                "onto": onto,
                "message": f"replaying the chain onto {onto[:12]} conflicts at {conflict.source_pr if conflict else '?'}",
                "pending": [r.source_pr for r in result.commits if r.status == chain.PENDING],
                "clean_prefix_tip": result.tip,
                # Members the new base already carries (landed outside the queue): empty commits in the
                # clean prefix, or still pending; the agent keeps them empty rather than replaying them.
                "landed_on_base": result.landed_on_base,
            }
        ]

    # --- background preparation of open batches ----------------------------------------------------

    def _note_reason(self, reason: str) -> None:
        if reason not in self._reasons:
            self._reasons.append(reason)

    def _prepare_open_batches(self, now: float) -> None:
        """Create `prepare` tasks for quiet, sizeable open batches, whatever the head is doing.

        A `prepare` task builds an open batch on its own base (never on the head's tip) and repairs what
        fails, so that the batch is ready when it seals; its result is adopted into the members and the
        batch stays open. It runs beside the head's own tasks: those keep claim priority and
        `repair_concurrency` bounds the claims in total, so a second worker prepares while the first
        repairs the head. A preparation is superseded only when its batch seals or changes. It is one
        of the batch's two background kinds (`BACKGROUND_TASKS`): it waits while a worker holds the
        batch's `pre_rebase` task, since adopting it rewrites the chain that task works on, and it
        supersedes an unclaimed one, which `_pre_rebase` recreates from the prepared chain.
        """
        for task in self.store.rows("tasks", {"repository": self.repository, "state": TASK_OPEN, "kind": TASK_PREPARE}):
            batch = self.store.row("batches", {"id": task["batch_id"]})
            if batch is None or batch["state"] != BATCH_OPEN:
                closed = "sealed" if batch is not None and batch["state"] == BATCH_HEAD else "batch-closed"
                self._supersede_prepare(task["batch_id"], closed, now)
            elif batch["version"] != task["batch_version"] and not self._pre_rebased_prepare(task):
                self._supersede_prepare(task["batch_id"], "batch-changed", now, own_base_only=True)
            elif self._stale_prepare(task) and not self._active_claims(batch["id"], task_id=task["id"]):
                # Built on a base `main` has moved past: `_onto_canonical` replays the batch first.
                self._supersede_prepare(task["batch_id"], "base-behind", now, own_base_only=True)
        for task in self.store.rows(
            "tasks", {"repository": self.repository, "state": TASK_OPEN, "kind": TASK_REBASE_CONFLICT}
        ):
            batch = self.store.row("batches", {"id": task["batch_id"]})
            if (
                batch is not None
                and batch["state"] == BATCH_OPEN
                and self._eviction(batch) is None
                and batch["version"] != task["batch_version"]
                and not self._active_claims(batch["id"], task_id=task["id"])
            ):
                # The batch changed under an unclaimed rebase onto `main`: `_onto_canonical` replays anew.
                with self.store.transaction():
                    self.store.update(
                        "tasks", {"id": task["id"]}, state=TASK_SUPERSEDED, resolved_at=now, resolution="batch-changed"
                    )
        if not self.setting("prepare_open_batches") or self._state("inference_hold"):
            return
        staging = self._staging_batches()
        for index, batch in enumerate(staging):
            successor = self._open_batch()
            if (
                self.setting("prepare_pre_rebased")
                and successor
                and successor["id"] == batch["id"]
                and self._pre_rebase_blocked(self._head()) is None
            ):
                # Prepare this successor on its intended parent. Starting another
                # own-base pass would serialize two full builds and rewrite repairs.
                continue
            if self._retained_tasks(batch):
                continue  # its retained work as the evicted head owns the chain
            if (
                index > 0
                and self._eviction(staging[index - 1]) is not None
                and batch["pre_rebase_state"] in PRE_REBASE_USABLE
            ):
                # Behind an evicted batch: its pre-rebase onto that batch's tip is carried when the
                # evicted batch returns; an own-base preparation would rewrite the chain under it.
                continue
            if not self._prepare_due(batch, now):
                if batch["prepared_version"] == batch["version"]:
                    # Prepared on a base `main` has moved past: kept when its replay onto `main` is clean,
                    # rebased onto `main` in the background (and then prepared again) when it conflicts.
                    self._onto_canonical(batch, now, rebase_clean=False)
                continue
            pre_rebase = self._open_pre_rebase_task(batch)
            if pre_rebase is not None and self._active_claims(batch["id"], task_id=pre_rebase["id"]):
                self._note_reason(
                    f"open batch {batch['id']}: preparation waits for the claimed pre_rebase task {pre_rebase['id']}"
                )
                continue
            pre_rebased = self._open_pre_rebased_prepare(batch)
            if pre_rebased is not None and self._active_claims(batch["id"], task_id=pre_rebased["id"]):
                self._note_reason(
                    f"open batch {batch['id']}: preparation waits for the claimed preparation of its pre-rebased "
                    f"chain (task {pre_rebased['id']})"
                )
                continue
            if not self._onto_canonical(batch, now):
                continue
            batch = self.store.row("batches", {"id": batch["id"]})
            if pre_rebase is not None:
                with self.store.transaction():
                    self._supersede_pre_rebase_tasks(batch["id"], "prepare", now)
                    self.store.journal(
                        now, "pre-rebase-superseded", batch["id"], task_id=pre_rebase["id"], resolution="prepare"
                    )
            self._create_task(batch, TASK_PREPARE, batch["base"], [], now)
            self._note_reason(f"open batch {batch['id']} waits for a worker to prepare it")

    def _stale_prepare(self, task: dict[str, Any]) -> bool:
        """An own-base `prepare` task on a base other than the canonical commit."""
        return (
            task["kind"] == TASK_PREPARE
            and not self._pre_rebased_prepare(task)
            and bool(self._canonical)
            and task["base"] != self._canonical
        )

    def _onto_canonical(self, batch: dict[str, Any], now: float, *, rebase_clean: bool = True) -> bool:
        """Whether the open batch is based on the canonical commit, replaying it there first when it is
        not: it lands on top of `main`, so a preparation on an older base builds a tree that never
        lands. The replay runs without the controller lock, under a `rebase` claim on the batch that
        holds insertion off, and is applied only if neither the batch nor the canonical commit moved
        meanwhile. A clean replay re-bases the batch (members' commits, base, tip, a new version). A
        conflicting one creates the batch's `rebase_conflict` task onto `main`, which a worker
        resolves in the background; while it is open the replay is not repeated and the batch gets no
        own-base preparation. A batch with a pre-rebase is left as it is: its preparation is that of the
        pre-rebased chain, which names its own chain. Without `rebase_clean` (a batch already prepared)
        a clean replay leaves the batch and its preparation as they are, and is recorded per batch
        version and canonical commit so it is not repeated."""
        canonical = self._canonical
        if not canonical or batch["base"] == canonical:
            return True
        if batch["pre_rebase_state"]:
            return False
        if self._open_rebase_task(batch) is not None:
            self._note_reason(f"open batch {batch['id']} waits for a worker to resolve its rebase onto main")
            return False
        key = f"open_rebase_clean:{batch['id']}"
        recorded = self._state(key) or {}
        if recorded.get("version") == batch["version"] and recorded.get("onto") == canonical:
            return True  # a clean replay of a prepared batch, left as it is
        if self._active_claims(batch["id"]):
            return False
        with self.store.transaction():
            claim = self._take_claim(batch, "rebase", now)
        chain_id = f"open-rebase-{batch['id']}-{secrets.token_hex(4)}"
        result: chain.ChainResult | None = None
        try:
            with self._lock.unlocked():
                result = chain.replay(self.clone, batch["base"], batch["tip"], canonical, chain_id=chain_id)
        except chain.ChainError as error:
            self._note_reason(f"open batch {batch['id']}: replay onto {canonical[:12]} failed: {error}")
        finally:
            chain.delete_chain_ref(self.clone, chain_id)
        current = self.store.row("batches", {"id": batch["id"]})
        moved = (
            result is None
            or current is None
            or current["version"] != batch["version"]
            or current["state"] != BATCH_OPEN
            or current["pre_rebase_state"]
            or self._canonical != canonical
            or [c for c in self._active_claims(batch["id"]) if c["id"] != claim["id"]]
        )
        if not moved and result.status == chain.CLEAN and not rebase_clean:
            with self.store.transaction():
                self._release_claim(claim, "clean", now)
                self._set_state(key, {"version": batch["version"], "onto": canonical})
            return True
        if moved or result.status != chain.CLEAN:
            with self.store.transaction():
                self._release_claim(claim, "batch-moved" if moved else "conflict", now)
                if not moved:
                    source_pr = result.conflict.source_pr if result.conflict else None
                    self.store.journal(now, "open-rebase-conflict", batch["id"], onto=canonical, source_pr=source_pr)
            if not moved:
                # A worker resolves the conflicts onto `main` in the background, as for the head; the
                # open task is what keeps the replay from repeating. Adopting the result re-bases the
                # batch, which is then prepared on `main`.
                self._create_task(
                    batch, TASK_REBASE_CONFLICT, canonical, self._conflict_diagnostics(result, canonical), now
                )
            return False
        version = batch["version"] + 1
        by_pr = {record.source_pr: record for record in result.commits}
        with self.store.transaction():
            for member in self._members(batch["id"]):
                record = by_pr.get(member["source_pr"])
                if record is not None:
                    self.store.update("members", {"id": member["id"]}, commit_sha=record.sha)
            self.store.update("batches", {"id": batch["id"]}, version=version, base=canonical, tip=result.tip)
            self._record_tip(batch["id"], version, result.tip, now)
            self._retain(self.batch_ref(batch["id"], version), result.tip)
            self._release_claim(claim, "rebased", now)
            self._count("open_batch_rebases")
            self.store.journal(
                now,
                "open-rebased",
                batch["id"],
                version=version,
                base=canonical,
                tip=result.tip,
                landed_on_base=result.landed_on_base,
            )
        return True

    def _pre_rebase_waits_for_prepare(self, batch: dict[str, Any]) -> str | None:
        """A live own-base owner finishes first; future work uses the intended parent."""
        prepare = self._open_own_base_prepare(batch)
        if prepare is not None:
            held = "claimed" if self._active_claims(batch["id"], task_id=prepare["id"]) else "open"
            return f"prepare task {prepare['id']} is {held}"
        return None

    def _prepare_outcome(self, batch: dict[str, Any]) -> dict[str, Any] | None:
        """The `prepare_errors` diagnostic of a batch prepared at its current version: the errors its
        preparation left (the `prepare` task's own diagnostics and the `remaining_errors` the worker
        reported with the adopted submission), or None when it left none."""
        if batch["prepared_version"] is None or batch["prepared_version"] != batch["version"]:
            return None
        task = self.store.row(
            "tasks",
            {"batch_id": batch["id"], "kind": TASK_PREPARE, "state": TASK_SUBMITTED, "pre_rebase_source": None},
            order="id DESC",
        )
        if task is None:
            return None
        errors = [
            d
            for d in loads(task["diagnostics"], [])
            if isinstance(d, dict) and d.get("kind") != "admission_predictions"
        ]
        submission = self.store.row("submissions", {"task_id": task["id"], "accepted": 1}, order="id DESC")
        try:
            notes = loads(submission["worker_notes"], {}) if submission is not None else {}
        except ValueError:
            notes = {}  # free-text notes carry no error list
        reported = notes.get("remaining_errors") if isinstance(notes, dict) else None
        if isinstance(reported, list):
            errors += [e for e in reported if isinstance(e, dict) and e not in errors]
        if not errors:
            return None
        return {
            "kind": "prepare_errors",
            "task_id": task["id"],
            "resolution": task["resolution"],
            "version": batch["version"],
            "errors": errors[:PREPARE_ERRORS_LIMIT],
            "omitted": max(0, len(errors) - PREPARE_ERRORS_LIMIT),
        }

    def _prepare_due(self, batch: dict[str, Any], now: float) -> bool:
        """The batch is big enough, quiet long enough, not prepared at this version, and has no
        preparation open or settled (adopted, clean or failed) at this version."""
        if batch["prepared_version"] == batch["version"]:
            return False
        members = self._members(batch["id"])
        if not members:
            return False
        predicted = self._predicted_collisions(self._admission_context(batch, now))
        digest = admission_planning.fingerprint([{"base": batch["base"]}, *predicted])
        attempted = False
        if predicted and self._state(f"admission_prepare:{batch['id']}") == digest:
            # A superseded offer nobody claimed is not an attempted preparation, including
            # offers recorded before this distinction existed. Keep eager work available.
            for task in self.store.rows(
                "tasks", {"batch_id": batch["id"], "kind": TASK_PREPARE, "pre_rebase_source": None}
            ):
                if not task["claims"]:
                    continue
                collisions = [
                    record
                    for diagnostic in loads(task["diagnostics"], [])
                    if diagnostic.get("kind") == "admission_predictions"
                    for record in self._predicted_collisions(diagnostic)
                ]
                if admission_planning.fingerprint([{"base": task["base"]}, *collisions]) == digest:
                    attempted = True
                    break
        if not batch["sealed"] and (not predicted or attempted):
            if len(members) < max(1, int(self.setting("prepare_min_members"))):
                return False
            if now - max(m["added_at"] for m in members) < int(self.setting("prepare_quiet_seconds")):
                return False
        for task in self.store.rows(
            "tasks",
            {
                "batch_id": batch["id"],
                "batch_version": batch["version"],
                "kind": TASK_PREPARE,
                "pre_rebase_source": None,
            },
        ):
            if task["state"] == TASK_OPEN or task["resolution"] in PREPARE_SETTLED:
                return False
        return True

    def _supersede_prepare(self, batch_id: int, resolution: str, now: float, *, own_base_only: bool = False) -> None:
        """Supersede the batch's open `prepare` tasks (with `own_base_only`, not the preparation of its
        pre-rebased chain) and release their claims."""
        where: dict[str, Any] = {"batch_id": batch_id, "state": TASK_OPEN, "kind": TASK_PREPARE}
        if own_base_only:
            where["pre_rebase_source"] = None
        tasks = self.store.rows("tasks", where)
        if not tasks:
            return
        with self.store.transaction():
            for task in tasks:
                self.store.update(
                    "tasks", {"id": task["id"]}, state=TASK_SUPERSEDED, resolved_at=now, resolution=resolution
                )
                for claim in self._active_claims(batch_id, task_id=task["id"]):
                    self._release_claim(claim, "superseded", now)
                self.store.journal(now, "prepare-superseded", batch_id, task_id=task["id"], resolution=resolution)

    # --- pre-rebase of the next batch --------------------------------------------------------------

    def _pre_rebase_blocked(self, head: dict[str, Any] | None) -> str | None:
        """Why the oldest open batch may not be replayed onto the head yet; None once it may.

        Only a head whose chain is submitted and validated and is waiting for (or has) its CI verdict
        qualifies: it is based on the canonical commit (rebased, or adopted from a task), has no open
        task and no claim, and its current version has a dispatched attempt that has not failed on
        the source. Before that the head's own conflicts and build are unsettled, and a replay onto
        it would inherit them; after a source failure its tip is about to change. The stacked pair is
        never built here, and nothing done to the open batch is a CI verdict.
        """
        if head is None:
            return "no head"
        if not self.setting("pre_rebase_enabled"):
            return "pre_rebase_enabled is off"
        if head["base"] != self._canonical:
            return f"head {head['id']} is not yet based on {self.canonical_branch}"
        task = self._open_task(head)
        speculative = task is not None and self._prediction_task(task)
        if task is not None and not speculative:
            return f"head {head['id']} has an open task"
        if self._active_claims(head["id"]) and not speculative:
            return f"head {head['id']} is claimed"
        attempt = self._current_attempt(head)
        if (
            attempt is None
            or attempt["head_sha"] != head["tip"]
            or attempt["dispatch_state"] in {"prepared", "rejected"}
        ):
            return f"head {head['id']} v{head['version']} is not dispatched"
        if attempt["outcome"] not in (OUTCOME_PENDING, OUTCOME_PASS, OUTCOME_INFRASTRUCTURE):
            return f"head {head['id']} CI attempt {attempt['id']} is {attempt['outcome']}"
        return None

    def _pre_rebase_target(self) -> str | None:
        """What the oldest open batch is pre-rebased onto: the head's tip, or `main` when no head is left."""
        head = self._head()
        return head["tip"] if head is not None else self._canonical

    def _open_pre_rebase_task(self, batch: dict[str, Any]) -> dict[str, Any] | None:
        return self.store.row(
            "tasks", {"batch_id": batch["id"], "state": TASK_OPEN, "kind": TASK_PRE_REBASE}, order="id DESC"
        )

    def _pre_rebase_task_current(self, task: dict[str, Any], batch: dict[str, Any]) -> bool:
        """A `pre_rebase` task still describes its batch: open, the batch open with the task's chain as
        a prefix (insertion only appends), and `onto` still the head's tip."""
        return (
            task["state"] == TASK_OPEN
            and batch["state"] == BATCH_OPEN
            and chain.is_ancestor(self.clone, task["tip"], batch["tip"])
            and task["onto"] in (self._pre_rebase_target(), self._preserved_onto(task))
        )

    def _set_pre_rebase(
        self,
        batch: dict[str, Any],
        state: str,
        now: float,
        *,
        tip: str | None = None,
        onto: str | None = None,
        source: str | None = None,
        edits: dict[str, Any] | None = None,
        prepared: bool = False,
    ) -> None:
        """Record (or with an empty `state`, clear) a batch's pre-rebase, keeping its chain under
        `pre_rebase_ref` in the clone and dropping the previous one. `pre_rebase_prepared` names the
        tip when `prepared` (a preparation of the chain was adopted) and is kept while the tip is the
        one prepared; any other tip is unprepared."""
        current = self.store.row("batches", {"id": batch["id"]}) or batch
        kept = current.get("pre_rebase_prepared")
        prepared_tip = tip if prepared or (tip and kept == tip) else None
        previous = (
            self.pre_rebase_ref(batch["id"], current["pre_rebase_onto"])
            if current.get("pre_rebase_tip") and current.get("pre_rebase_onto")
            else None
        )
        ref = self.pre_rebase_ref(batch["id"], onto) if tip and onto else None
        if previous and previous != ref:
            chain._git(self.clone, "update-ref", "-d", previous)
        if ref and tip:
            self._retain_local(ref, tip)
        self.store.update(
            "batches",
            {"id": batch["id"]},
            pre_rebase_state=state,
            pre_rebase_tip=tip,
            pre_rebase_onto=onto,
            pre_rebase_source=source,
            pre_rebase_edits=dumps(edits or {}),
            pre_rebase_at=now if state else None,
            pre_rebase_prepared=prepared_tip if state else None,
        )

    def _supersede_pre_rebase_tasks(self, batch_id: int, resolution: str, now: float) -> int:
        superseded = 0
        for task in self.store.rows("tasks", {"batch_id": batch_id, "state": TASK_OPEN, "kind": TASK_PRE_REBASE}):
            self.store.update(
                "tasks", {"id": task["id"]}, state=TASK_SUPERSEDED, resolved_at=now, resolution=resolution
            )
            for claim in self._active_claims(batch_id, task_id=task["id"]):
                self._release_claim(claim, "superseded", now)
            superseded += 1
        return superseded

    def _discard_pre_rebase(self, reason: str, now: float, reshaped: dict[str, Any]) -> None:
        """`reshaped` was reshaped (rebuilt, emptied, removed, or a member shrunk to nothing): forget the
        open batches' pre-rebases onto any of its recorded tips, edits included, and supersede their
        tasks; the next pass replays from scratch. In normal operation only the first open batch has a
        pre-rebase, onto the head; a pre-rebase onto another batch's tip (the emergency lane) is kept."""
        tips = {row["tip"] for row in self.store.rows("batch_tips", {"batch_id": reshaped["id"]})} | {reshaped["tip"]}
        for batch in self._open_batches():
            if not batch["pre_rebase_state"] and self._open_pre_rebase_task(batch) is None:
                continue
            task = self._open_pre_rebase_task(batch)
            onto = batch["pre_rebase_onto"] if batch["pre_rebase_state"] else (task or {}).get("onto")
            if batch["id"] != reshaped["id"] and onto not in tips:
                continue
            with self.store.transaction():
                self._set_pre_rebase(batch, "", now)
                superseded = self._supersede_pre_rebase_tasks(batch["id"], reason, now)
                self.store.journal(now, "pre-rebase-discarded", batch["id"], reason=reason, tasks_superseded=superseded)

    def _supersede_stale_pre_rebase(self, now: float) -> None:
        """Supersede (releasing its claims) a `pre_rebase` task whose `onto` is no longer the head's tip,
        or whose batch has moved on; the replay is redone by `_pre_rebase` when the head allows it."""
        for task in self.store.rows("tasks", {"state": TASK_OPEN, "kind": TASK_PRE_REBASE}):
            batch = self.store.row("batches", {"id": task["batch_id"]})
            if batch is None or batch["state"] != BATCH_OPEN or self._pre_rebase_task_current(task, batch):
                continue
            if self._preserve_across_head_round(task, batch, now):
                continue
            with self.store.transaction():
                self._supersede_pre_rebase_tasks(batch["id"], "head-moved", now)
                self.store.journal(now, "pre-rebase-superseded", batch["id"], task_id=task["id"], onto=task["onto"])
        self._supersede_stale_pre_rebased_prepare(now)

    def _preserve_across_head_round(self, task: dict[str, Any], batch: dict[str, Any], now: float) -> bool:
        """Keep a claimed `pre_rebase` task whose `onto` is an earlier tip of the current head (a repair
        round or a rebase moved it) instead of superseding it: the agent's resolution takes longer
        than a head round, and restarting it on every round means it never finishes. It is recorded
        in `state:emergency_preserved` like the claimed successor work of an emergency head, so its
        owner may still submit onto that tip and the next `_pre_rebase` carries the adopted result
        onto the head's tip by replay (`_carry_pre_rebase`). True when preserved."""
        head = self._head()
        if (
            head is None
            or not self._active_claims(batch["id"], task_id=task["id"])
            or not chain.is_ancestor(self.clone, task["tip"], batch["tip"])
            or self.store.row("batch_tips", {"batch_id": head["id"], "tip": task["onto"]}) is None
        ):
            return False
        preserved = self._state("emergency_preserved") or {}
        preserved[str(task["id"])] = task["onto"]
        self._set_state("emergency_preserved", preserved)
        self.store.journal(now, "pre-rebase-preserved", batch["id"], task_id=task["id"], onto=task["onto"])
        return True

    def _pre_rebase(self, now: float) -> None:
        """Replay the oldest open batch onto the head's tip in the background, once the head allows it.

        The batch stays open on its own base; the replayed chain is recorded beside it (`pre_rebase_*`)
        and kept under `pre_rebase_ref`. A usable earlier pre-rebase (clean, or adopted from a task, so
        with the agent's resolution) is carried forward: replayed onto the head's new tip when that
        moved (another repair round, a rebase of the head) and extended by the members inserted since.
        When that does not apply cleanly, or there is none, the batch's own chain is replayed onto the
        head's tip; a conflict becomes a `pre_rebase` task with the conflict evidence and the predicted
        cross-batch ripples. `_seal_from_pre_rebase` uses the result when the head lands.
        """
        self._supersede_stale_pre_rebase(now)
        batch = self._open_batch()
        if batch is None or not self._members(batch["id"]):
            return
        if self._preserved_tasks(batch):
            self._note_reason(f"open batch {batch['id']} waits for claimed pre-rebase work")
            return
        if self._retained_tasks(batch):
            self._note_reason(f"open batch {batch['id']} waits for its retained work as the evicted head")
            return
        if self._open_rebase_task(batch) is not None:
            self._note_reason(f"open batch {batch['id']} waits for its rebase onto main")
            return
        head = self._head()
        if self._pre_rebase_blocked(head) is not None:
            return
        assert head is not None
        onto = head["tip"]
        own_prepare = self._open_own_base_prepare(batch)
        if own_prepare and not self._active_claims(batch["id"], task_id=own_prepare["id"]):
            self._supersede_prepare(batch["id"], "pre-rebase", now, own_base_only=True)
        state = batch["pre_rebase_state"]
        if self._open_pre_rebase_task(batch) is not None:
            self._reasons.append(f"open batch {batch['id']} waits for a worker to resolve its pre-rebase")
            return
        waiting = self._pre_rebase_waits_for_prepare(batch)
        if batch["pre_rebase_onto"] == onto and batch["pre_rebase_source"] == batch["tip"] and state:
            if state != PRE_REBASE_CONFLICT:
                return  # up to date
            if waiting is not None:
                self._note_reason(f"open batch {batch['id']}: pre_rebase task waits for its preparation ({waiting})")
                return
            # A conflict recorded while the batch waited for its preparation, which then found nothing
            # to change: replayed again below and handed to a `pre_rebase` task.
        refs: list[str] = []

        def replay(base: str, tip: str, target: str) -> chain.ChainResult:
            chain_id = f"pre-rebase-{batch['id']}-{secrets.token_hex(4)}"
            refs.append(chain_id)
            return chain.replay(self.clone, base, tip, target, chain_id=chain_id)

        try:
            carried = self._carry_pre_rebase(batch, onto, replay)
            result = None if carried is not None else replay(batch["base"], batch["tip"], onto)
        except chain.ChainError as error:
            self.store.journal(now, "pre-rebase-error", batch["id"], onto=onto, error=str(error))
            self._reasons.append(f"pre-rebase of open batch {batch['id']} failed: {error}")
            return
        finally:
            for chain_id in refs:
                chain.delete_chain_ref(self.clone, chain_id)
        if carried is not None:
            tip, edits = carried
            with self.store.transaction():
                self._set_pre_rebase(
                    batch, batch["pre_rebase_state"], now, tip=tip, onto=onto, source=batch["tip"], edits=edits
                )
                self.store.journal(now, "pre-rebased", batch["id"], onto=onto, tip=tip, carried=True)
            return
        assert result is not None
        if result.status == chain.CLEAN:
            with self.store.transaction():
                self._set_pre_rebase(batch, PRE_REBASE_CLEAN, now, tip=result.tip, onto=onto, source=batch["tip"])
                self._count("pre_rebases")
                self.store.journal(now, "pre-rebased", batch["id"], onto=onto, tip=result.tip, carried=False)
            return
        conflict = result.conflict
        diagnostics = [
            {
                "kind": "conflict",
                "source_pr": conflict.source_pr if conflict else None,
                "commit": conflict.replayed if conflict else None,
                "paths": list(conflict.conflict_paths) if conflict else [],
                "onto": onto,
                "message": (
                    f"replaying the open batch onto the head's tip {onto[:12]} conflicts at "
                    f"{conflict.source_pr if conflict else '?'}"
                ),
                "pending": [r.source_pr for r in result.commits if r.status == chain.PENDING],
                "clean_prefix_tip": result.tip,
                "landed_on_base": result.landed_on_base,
            },
            self._cross_batch(head, batch),
        ]
        with self.store.transaction():
            self._set_pre_rebase(batch, PRE_REBASE_CONFLICT, now, onto=onto, source=batch["tip"])
            self.store.journal(
                now,
                "pre-rebase-conflict",
                batch["id"],
                onto=onto,
                source_pr=conflict.source_pr if conflict else None,
                landed_on_base=result.landed_on_base,
                waits_for_prepare=waiting,
            )
        if waiting is not None:
            # The agent resolves the conflict on the prepared chain: a preparation adopted later would
            # rewrite the chain under it. The conflict is replayed again once the batch is prepared.
            self._note_reason(f"open batch {batch['id']}: pre_rebase task waits for its preparation ({waiting})")
            return
        prepared = self._prepare_outcome(batch)
        if prepared is not None:
            diagnostics.append(prepared)
        self._create_task(batch, TASK_PRE_REBASE, onto, diagnostics, now)
        self._reasons.append(f"open batch {batch['id']} needs an agent to resolve its pre-rebase onto the head")

    def _carry_pre_rebase(
        self, batch: dict[str, Any], onto: str, replay: Callable[[str, str, str], chain.ChainResult]
    ) -> tuple[str, dict[str, Any]] | None:
        """Bring a usable pre-rebase up to date (onto `onto`, covering the batch's tip) by replay alone;
        the new tip and the kept edits, or None when there is none or a replay conflicts."""
        if batch["pre_rebase_state"] not in PRE_REBASE_USABLE or not batch["pre_rebase_tip"]:
            return None
        source = batch["pre_rebase_source"]
        if not source or not chain.is_ancestor(self.clone, source, batch["tip"]):
            return None  # the batch's own chain was rewritten under it
        tip = batch["pre_rebase_tip"]
        if batch["pre_rebase_onto"] != onto:
            moved = replay(batch["pre_rebase_onto"], tip, onto)
            if moved.status != chain.CLEAN:
                return None
            tip = moved.tip
        if source != batch["tip"]:
            extended = replay(source, batch["tip"], tip)
            if extended.status != chain.CLEAN:
                return None
            tip = extended.tip
        return tip, loads(batch["pre_rebase_edits"], {})

    # --- preparation of the pre-rebased chain ------------------------------------------------------

    @staticmethod
    def _pre_rebased_prepare(task: dict[str, Any]) -> bool:
        """A `prepare` task of a batch's pre-rebased chain (on the head's tip), not of its own chain."""
        return task["kind"] == TASK_PREPARE and bool(task.get("pre_rebase_source"))

    def _open_own_base_prepare(self, batch: dict[str, Any]) -> dict[str, Any] | None:
        return self.store.row(
            "tasks",
            {"batch_id": batch["id"], "state": TASK_OPEN, "kind": TASK_PREPARE, "pre_rebase_source": None},
            order="id DESC",
        )

    def _open_pre_rebased_prepare(self, batch: dict[str, Any]) -> dict[str, Any] | None:
        for task in self.store.rows(
            "tasks", {"batch_id": batch["id"], "state": TASK_OPEN, "kind": TASK_PREPARE}, order="id DESC"
        ):
            if self._pre_rebased_prepare(task):
                return task
        return None

    def _pre_rebased_prepare_current(self, task: dict[str, Any], batch: dict[str, Any]) -> bool:
        """A `prepare` task of the pre-rebased chain still describes its batch: open, the batch open
        with a usable pre-rebase on the task's `onto`, which is still the head's tip, and the task's
        chain a prefix of that pre-rebase (insertion extends it by replay, which keeps the prefix)."""
        return (
            task["state"] == TASK_OPEN
            and batch["state"] == BATCH_OPEN
            and batch["pre_rebase_state"] in PRE_REBASE_USABLE
            and bool(batch["pre_rebase_tip"])
            and batch["pre_rebase_onto"] == task["onto"]
            and task["onto"] in (self._pre_rebase_target(), self._preserved_onto(task))
            and chain.is_ancestor(self.clone, task["tip"], batch["pre_rebase_tip"])
        )

    def _supersede_stale_pre_rebased_prepare(self, now: float) -> None:
        """Supersede (releasing its claims) a preparation of a pre-rebased chain that no longer
        describes its open batch: the head's tip changed (a repair round, a rebase of the head), or the
        pre-rebase was redone, discarded or hit a conflict. A sealed batch's is superseded by `_seal`."""
        for task in self.store.rows("tasks", {"state": TASK_OPEN, "kind": TASK_PREPARE}):
            if not self._pre_rebased_prepare(task):
                continue
            batch = self.store.row("batches", {"id": task["batch_id"]})
            if batch is None or batch["state"] != BATCH_OPEN or self._pre_rebased_prepare_current(task, batch):
                continue
            moved = task["onto"] != self._pre_rebase_target()
            resolution = "head-moved" if moved else "pre-rebase-changed"
            with self.store.transaction():
                self.store.update(
                    "tasks", {"id": task["id"]}, state=TASK_SUPERSEDED, resolved_at=now, resolution=resolution
                )
                for claim in self._active_claims(batch["id"], task_id=task["id"]):
                    self._release_claim(claim, "superseded", now)
                self.store.journal(
                    now, "prepare-superseded", batch["id"], task_id=task["id"], resolution=resolution, pre_rebased=True
                )

    def _pre_rebased_prepare_blocked(self, batch: dict[str, Any]) -> str | None:
        """Why the batch's pre-rebased chain gets no `prepare` task now; None once it may.

        The chain must be a usable pre-rebase that is up to date (on the head's tip, covering the
        batch's tip) and not yet prepared, on a head `_pre_rebase_blocked` allows (its tip moves only
        after a failed CI round), for a batch that own-base preparation applies to and that is prepared
        at its current version (`_pre_rebase_waits_for_prepare`: a later own-base preparation would
        rewrite the chain under it). No other background task of the batch may be open, and a
        preparation of the same chain that was adopted, released clean or failed is not repeated."""
        if not self.setting("prepare_pre_rebased") or not self.setting("prepare_open_batches"):
            return "prepare_pre_rebased is off"
        if self._preserved_tasks(batch):
            return "waits for claimed pre-rebase work"
        if self._retained_tasks(batch):
            return "waits for its retained work as the evicted head"
        if not batch["sealed"] and len(self._members(batch["id"])) < max(1, int(self.setting("prepare_min_members"))):
            return "below prepare_min_members"
        if batch["pre_rebase_state"] not in PRE_REBASE_USABLE or not batch["pre_rebase_tip"]:
            return "no usable pre-rebase"
        blocked = self._pre_rebase_blocked(self._head())
        if blocked is not None:
            return blocked
        if batch["pre_rebase_onto"] != self._pre_rebase_target() or batch["pre_rebase_source"] != batch["tip"]:
            return "the pre-rebase is not up to date"
        if batch["pre_rebase_prepared"] == batch["pre_rebase_tip"]:
            return "prepared"
        waiting = self._pre_rebase_waits_for_prepare(batch)
        if waiting is not None:
            return waiting
        for kind in BACKGROUND_TASKS:
            if self.store.row("tasks", {"batch_id": batch["id"], "state": TASK_OPEN, "kind": kind}) is not None:
                return f"a {kind} task is open"
        for task in self.store.rows(
            "tasks",
            {
                "batch_id": batch["id"],
                "kind": TASK_PREPARE,
                "tip": batch["pre_rebase_tip"],
                "onto": batch["pre_rebase_onto"],
            },
        ):
            if task["resolution"] in PREPARE_SETTLED:
                return f"prepare task {task['id']} was {task['resolution']}"
        return None

    def _prepare_pre_rebased(self, now: float) -> None:
        """Create a `prepare` task for the oldest open batch's pre-rebased chain on the head's tip.

        The worker builds that chain warm from the outputs published for the head's tip, repairs what
        fails in place, runs the audits, submits, and publishes the outputs of the tip it submitted;
        `_adopt_prepared_pre_rebase` records the result as the batch's pre-rebase, prepared on that
        tip. A head that lands as that tip seals with the chain unchanged, so its CI round fetches the
        published outputs for its exact tip. The task is a background task like any other (one claim
        per batch, `repair_concurrency - 1` in total), superseded when the head's tip changes, and
        sealing never waits for it."""
        self._supersede_stale_pre_rebased_prepare(now)
        if self._state("inference_hold"):
            return
        batch = self._open_batch()
        if batch is None or not self._members(batch["id"]):
            return
        if self._pre_rebased_prepare_blocked(batch) is not None:
            return
        tip = batch["pre_rebase_tip"]
        self._retain(self.pre_rebased_ref(batch["id"], tip), tip)
        head = self._head()
        diagnostics = [self._cross_batch(head, batch)] if head is not None else []
        self._create_task(
            batch, TASK_PREPARE, batch["pre_rebase_onto"], diagnostics, now, pre_rebased=(tip, batch["tip"])
        )
        self._note_reason(f"open batch {batch['id']} waits for a worker to prepare its pre-rebased chain")

    def _settle_pre_rebased_release(
        self, task: dict[str, Any], batch: dict[str, Any] | None, reason: str, now: float
    ) -> None:
        """A released preparation of a pre-rebased chain: `prepared_clean` records the chain as
        prepared (when it is still the batch's pre-rebase), a failure settles the task for that chain."""
        if reason == PREPARE_CLEAN_REASON:
            self.store.update("tasks", {"id": task["id"]}, state=TASK_SUBMITTED, resolved_at=now, resolution="prepared")
            if (
                batch is not None
                and batch["pre_rebase_tip"] == task["tip"]
                and self._pre_rebased_prepare_current(task, batch)
            ):
                self.store.update("batches", {"id": batch["id"]}, pre_rebase_prepared=task["tip"])
            self._count("pre_rebased_prepares_clean")
            self.store.journal(
                now, "prepared", task["batch_id"], task_id=task["id"], tip=task["tip"], clean=True, pre_rebased=True
            )
        elif reason in PREPARE_FAILED_REASONS:
            self.store.update("tasks", {"id": task["id"]}, state=TASK_SUPERSEDED, resolved_at=now, resolution="failed")
            self._count("pre_rebased_prepares_failed")
            self.store.journal(
                now, "prepare-failed", task["batch_id"], task_id=task["id"], reason=reason, pre_rebased=True
            )

    def _adopt_prepared_pre_rebase(
        self,
        task: dict[str, Any],
        claim: dict[str, Any],
        batch: dict[str, Any],
        tip_sha: str,
        comparison: chain.ComparisonReport,
        worker_notes: str,
        now: float,
    ) -> Adoption:
        """Record a prepared pre-rebased chain as the open batch's pre-rebase, prepared on the head's tip.

        Like `_adopt_pre_rebase`, the batch's own members, base, tip and version are unchanged; the
        members the agent edited join the pre-rebase's edits (explanations appended), which the seal
        that adopts the chain records (`_seal_from_pre_rebase`). The chain covers the batch tip the
        task was created for; members inserted since are replayed onto it by the next `_pre_rebase`.
        """
        edits = loads(batch["pre_rebase_edits"], {})
        for m in comparison.members:
            if m.provenance != chain.EDITED:
                continue
            prior = edits.get(m.source_pr) or {}
            explanation = (str(prior.get("explanation", "")) + "\n\n" + m.explanation.strip()).strip()
            edits[m.source_pr] = {"explanation": explanation, "task_id": task["id"]}
        provenance = {
            m["source_pr"]: "edited" if m["source_pr"] in edits else m["provenance"] for m in self._task_members(task)
        }
        ref = self.pre_rebase_ref(batch["id"], task["onto"])
        with self.store.transaction():
            self._set_pre_rebase(
                batch,
                PRE_REBASE_ADOPTED if edits else batch["pre_rebase_state"],
                now,
                tip=tip_sha,
                onto=task["onto"],
                source=task["pre_rebase_source"],
                edits=edits,
                prepared=True,
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
            self.store.insert(
                "submissions",
                task_id=task["id"],
                claim_id=claim["id"],
                tip=tip_sha,
                retained_ref=ref,
                accepted=1,
                reason="adopted",
                worker_notes=worker_notes,
                created_at=now,
            )
            self._count("pre_rebased_prepares_adopted")
            self.store.journal(
                now,
                "pre-rebase-prepared",
                batch["id"],
                task_id=task["id"],
                onto=task["onto"],
                tip=tip_sha,
                provenance=provenance,
            )
        self.report_inference(claim["worker"], True)
        self._wake.set()
        return Adoption(True, "adopted", task["id"], batch["id"], batch["version"], tip_sha, ref, provenance=provenance)

    def _cross_batch(self, head: dict[str, Any], batch: dict[str, Any]) -> dict[str, Any]:
        """The `cross_batch` diagnostic: names the head removes or renames that the batch's members use,
        and names both add (`repair_context.cross_batch`, approximate)."""
        from pathlib import Path

        from . import repair, repair_context

        clone = Path(self.clone)
        try:
            ahead = repair.read_chain(clone, head["base"], head["tip"])
            behind = repair.read_chain(clone, batch["base"], batch["tip"])
        except Exception as error:  # noqa: BLE001 - the task goes out without the prediction
            return {"kind": "cross_batch", "removed_used": [], "added_by_both": [], "error": str(error)}

        def records(batch_id: int) -> dict[int, dict]:
            return {
                (str(m["number"]) if isinstance(m["number"], SourceNumber) else m["number"]): loads(
                    (self.store.row("inputs", {"id": m["input_id"]}) or {}).get("declaration_names"), {}
                )
                for m in self._members(batch_id)
            }

        edited = {m["number"] for b in (head, batch) for m in self._members(b["id"]) if m["provenance"] != "clean"}
        return {
            "kind": "cross_batch",
            **repair_context.cross_batch(
                clone,
                ahead,
                behind,
                head_declaration_names=records(head["id"]),
                following_declaration_names=records(batch["id"]),
                edited_prs=edited,
            ),
        }

    def _seal_from_pre_rebase(self, head: dict[str, Any], canonical: str, now: float) -> bool:
        """Base a newly sealed head on `canonical` from its pre-rebase, without replaying it again.

        Applies when the head ahead of it landed as the tip the pre-rebase was made on (`main` equals
        `pre_rebase_onto`), or otherwise when the pre-rebase carries onto `main` by a clean replay;
        members inserted after the pre-rebase are replayed onto it first. The members take their
        commits from the pre-rebased chain, and those an agent edited in a `pre_rebase` task are
        recorded as edited with its explanation. False (nothing changed) when there is no such
        pre-rebase, a carry or an extension conflicts, or the chain's members do not match.
        """
        onto = head["pre_rebase_onto"]
        if head["pre_rebase_state"] not in PRE_REBASE_USABLE or not onto:
            return False
        tip = head["pre_rebase_tip"]
        source = head["pre_rebase_source"]
        if not tip or not source or not chain.is_ancestor(self.clone, source, head["tip"]):
            return False
        if onto != canonical:
            # A batch landed after the pre-rebase was made (a lane or emergency head went ahead of
            # this one while its pre-rebase work was claimed), or the head landed at a later tip than
            # the one a preserved task resolved onto (`_preserve_across_head_round`): carry the
            # pre-rebase, with its edits, onto `main` by replay, as `_carry_pre_rebase` does while a
            # head runs.
            chain_id = f"pre-rebase-carry-{head['id']}-{secrets.token_hex(4)}"
            try:
                moved = chain.replay(self.clone, onto, tip, canonical, chain_id=chain_id)
            except chain.ChainError as error:
                self.store.journal(now, "pre-rebase-error", head["id"], onto=canonical, error=str(error))
                return False
            finally:
                chain.delete_chain_ref(self.clone, chain_id)
            if moved.status != chain.CLEAN:
                return False
            tip = moved.tip
        landed_on_base: list[dict[str, str]] = []
        if source != head["tip"]:
            chain_id = f"pre-rebase-seal-{head['id']}-{secrets.token_hex(4)}"
            try:
                extended = chain.replay(self.clone, source, head["tip"], tip, chain_id=chain_id)
            except chain.ChainError as error:
                self.store.journal(now, "pre-rebase-error", head["id"], onto=canonical, error=str(error))
                return False
            finally:
                chain.delete_chain_ref(self.clone, chain_id)
            if extended.status != chain.CLEAN:
                return False
            tip, landed_on_base = extended.tip, extended.landed_on_base
        try:
            commits = chain.chain_commits(self.clone, canonical, tip)
        except chain.ChainError:
            return False
        by_pr = {commit.source_pr: commit for commit in commits}
        members = self._members(head["id"])
        if len(commits) != len(members) or set(by_pr) != {m["source_pr"] for m in members}:
            return False
        edits = loads(head["pre_rebase_edits"], {})
        version = head["version"] + 1
        landed = self._landed_between(head["base"], canonical) if edits else {}
        with self.store.transaction():
            for member in members:
                sha = by_pr[member["source_pr"]].sha
                values: dict[str, Any] = {"commit_sha": sha}
                edit = edits.get(member["source_pr"])
                if edit:
                    values.update(
                        provenance="edited",
                        task_id=edit.get("task_id"),
                        explanation=(member["explanation"] + "\n\n" + edit.get("explanation", "")).strip(),
                        edited_shas=dumps(loads(member["edited_shas"], []) + [sha]),
                    )
                self.store.update("members", {"id": member["id"]}, **values)
                if edit and self._commit_empty(sha) and not chain.landed_commit(landed, member["source_pr"]):
                    self.store.journal(
                        now,
                        "member-reduced-to-empty",
                        head["id"],
                        member_id=member["id"],
                        task_id=edit.get("task_id"),
                        commit=sha,
                    )
                if (
                    edit
                    and member["no_repair"]
                    and not member["withdrawn"]
                    and self._commit_empty(sha)
                    and not chain.landed_commit(landed, member["source_pr"])
                ):
                    self._anticipate(head, dict(member, **values), edit.get("explanation", ""), now)
            self.store.update(
                "batches", {"id": head["id"]}, version=version, base=canonical, tip=tip, expected_parent=None
            )
            self._record_tip(head["id"], version, tip, now)
            self._set_pre_rebase(head, "", now)
            for stale in self.store.rows("tasks", {"batch_id": head["id"], "state": TASK_OPEN}):
                self.store.update(
                    "tasks", {"id": stale["id"]}, state=TASK_SUPERSEDED, resolved_at=now, resolution="rebased"
                )
            self._retain(self.batch_ref(head["id"], version), tip)
            self._count("head_rebases")
            self._count("pre_rebases_used")
            self.store.journal(
                now,
                "rebased",
                head["id"],
                version=version,
                base=canonical,
                tip=tip,
                landed_on_base=landed_on_base,
                pre_rebased=True,
            )
        return True

    # --- CI ----------------------------------------------------------------------------------------

    def _advance_attempt(self, head: dict[str, Any], attempt: dict[str, Any], now: float) -> None:
        candidate_ci = self._candidate_coordinator(attempt_id=attempt["id"])
        if attempt.get("dispatch_state") == "prepared":
            if candidate_ci is not None and now < (attempt["dispatch_retry_at"] or 0):
                self._reasons.append(
                    f"head {head['id']}: prepared source CI retry at {attempt['dispatch_retry_at']:.0f}"
                )
                return
            self._send_prepared(head, attempt, now)
        else:
            self._observe_attempt(head, attempt, now)

    def _ci(self, head: dict[str, Any], now: float) -> None:
        attempt = self._current_attempt(head)
        if attempt is None or attempt["outcome"] == OUTCOME_SUPERSEDED:
            self._dispatch(head, now, retry=0)
            return
        if attempt["outcome"] == OUTCOME_PENDING:
            self._advance_attempt(head, attempt, now)
            attempt = self.store.row("ci_attempts", {"id": attempt["id"]}) or attempt
        if attempt["outcome"] == OUTCOME_PENDING:
            self._reasons.append(f"head {head['id']} waits for CI run {attempt['run_id'] or '(not yet found)'}")
        elif attempt["outcome"] == OUTCOME_INFRASTRUCTURE:
            failures = self.store.rows(
                "ci_attempts",
                {"batch_id": head["id"], "batch_version": head["version"], "outcome": OUTCOME_INFRASTRUCTURE},
            )
            retries = sum(a["dispatch_state"] != "rejected" for a in failures)
            limit = int(self.setting("infrastructure_failure_limit"))
            rejected = attempt["dispatch_state"] == "rejected"
            # Proven non-sends (`rejected`) are not counted: they cost no CI run. Once the limit is
            # reached (or a backoff begun on an earlier version is still running) the queue backs off
            # automatically; it never waits for an operator.
            if limit and not rejected and (retries >= limit or self._state("infrastructure_backoff")):
                if self._infrastructure_backoff(head, attempt, retries, now):
                    return
                self._dispatch(head, now, retry=retries)
                return
            # Proven non-sends need no CI-run retry budget. Keep retrying with bounded
            # backoff, respecting a backend's persisted rate-limit deadline.
            exponent = min(3, max(0, len(failures) - retries - 1 if rejected else retries - 1))
            backoff = int(self.setting("infrastructure_retry_backoff_seconds")) * (2 ** max(0, exponent))
            due = (attempt["last_observed_at"] or attempt["dispatched_at"]) + backoff
            due = max(due, attempt.get("dispatch_retry_at") or 0)
            if now < due:
                self._reasons.append(f"head {head['id']}: re-dispatch after infrastructure failure at {due:.0f}")
                return
            self._dispatch(head, now, retry=retries)
        elif attempt["outcome"] == OUTCOME_SOURCE_FAILURE:
            self._infrastructure_recovered(head, attempt, now)
            if self._open_task(head) is None:
                # The exact findings, unchanged: they carry any member attribution CI reported
                # (`source_pr`). However often a failure recurs, the head goes to repair again; only the
                # agent may withdraw a member, by reducing its contribution to an empty commit.
                diagnostics = loads(attempt["diagnostics"], [])
                self._create_task(head, TASK_REPAIR, head["base"], diagnostics, now, ci_attempt_id=attempt["id"])
            self._reasons.append(f"head {head['id']} waits for a worker to claim its repair task")
        elif attempt["outcome"] == OUTCOME_PASS:
            self._infrastructure_recovered(head, attempt, now)
            self._promote(head, attempt, now)

    def rebuild_head(self, repo: str, actor: str, *, source_access=None, source_refresh=None) -> dict[str, Any]:
        """Operator: rebuild the head's chain from its members' accepted heads and dispatch it again.

        For a head whose chain must be regenerated without a source change (a new commit message format,
        a chain tool fix): the open task is superseded, the members' commits are rebuilt on the head's
        base, the version advances so the next tick dispatches a fresh attempt. Refused while the head is
        claimed or when a member carries agent edits, which a rebuild from accepted heads would discard.
        """
        with self._lock:
            if self.source_authority is not None:
                access, _ = self._source_request_access(source_access)
                actor = principal_key(access.actor)
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
            if self.source_authority is not None:
                source_access = self._refresh_source_access(source_access, source_refresh)
                now = self.clock()
                if not self._source_head_current(head) or self._members(head["id"]) != members:
                    return {"rebuilt": False, "reason": "head-changed"}
                if self._active_claims(head["id"]):
                    return {"rebuilt": False, "reason": "claimed"}
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
                self._record_tip(head["id"], version, result.tip, now)
                superseded = 0
                for stale in self.store.rows("tasks", {"batch_id": head["id"], "state": TASK_OPEN}):
                    self.store.update(
                        "tasks", {"id": stale["id"]}, state=TASK_SUPERSEDED, resolved_at=now, resolution="rebuilt"
                    )
                    superseded += 1
                self._retain(self.batch_ref(head["id"], version), result.tip)
                self._count("head_rebuilds")
                self._discard_pre_rebase("head-rebuilt", now, head)
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
            self._cancel_superseded_runs(head["id"], now, reason=f"head rebuilt as version {version}")
            self._wake.set()
            return {
                "rebuilt": True,
                "version": version,
                "tip": result.tip,
                "tasks_superseded": superseded,
                "landed_on_base": result.landed_on_base,
            }

    def evict_native(self, repo: str, actor: str, *, withdraw: Sequence[int] = ()) -> dict[str, Any]:
        """Operator: break apart every live batch (the head and the open batches) that holds a Forgejo
        member or a GitHub PR named in `withdraw`, for when the Forgejo source cannot be used.

        Each such batch is removed (tasks superseded, claims released, pre-rebases discarded, runs
        cancelled) and all its members go back to waiting at their original age with their approvals kept
        (`_return_members`), so a Forgejo PR re-enters a batch by itself once
        `forgejo_placement_enabled` is true again. GitHub-only batches are kept. A PR in `withdraw` then
        has its input cancelled as `bors r-` would (a new approval brings it back). Refused while
        `forgejo_placement_enabled` is true, since the returned Forgejo inputs would be placed again."""
        with self._lock:
            if self.source_authority is not None:
                return {"evicted": False, "reason": "native-controller"}
            self._check_repo(repo)
            if self.setting("forgejo_placement_enabled"):
                return {"evicted": False, "reason": "placement-enabled"}
            named = {int(number) for number in withdraw}
            now = self.clock()
            batches = self.store.rows(
                "batches", {"repository": self.repository, "state": [BATCH_OPEN, BATCH_HEAD]}, order="id"
            )
            affected = [
                batch
                for batch in batches
                if any(
                    not member["withdrawn"]
                    and (isinstance(member["number"], SourceNumber) or member["number"] in named)
                    for member in self._members(batch["id"])
                )
            ]
            returned = 0
            # One transaction, with the lock held throughout: every affected batch is removed, its
            # members wait again and the named PRs are cancelled before anything lets the lock go, so
            # no insertion can place a returned input into a batch being removed or re-place a named PR.
            with self.store.transaction():
                for batch in affected:
                    members = self._members(batch["id"])
                    for stale in self.store.rows("tasks", {"batch_id": batch["id"], "state": TASK_OPEN}):
                        self.store.update(
                            "tasks",
                            {"id": stale["id"]},
                            state=TASK_SUPERSEDED,
                            resolved_at=now,
                            resolution="native-evicted",
                        )
                    for claim in self._active_claims(batch["id"]):
                        self._release_claim(claim, "superseded", now)
                    self._set_pre_rebase(batch, "", now)
                    self._return_members(batch, members, "native-evicted", now)
                    returned += sum(1 for member in members if not member["withdrawn"])
                    self.store.journal(
                        now, "native-evicted", batch["id"], actor=actor, state=batch["state"], members=len(members)
                    )
                    self._discard_pre_rebase("native-evicted", now, batch)
                    self.store.update("batches", {"id": batch["id"]}, state=BATCH_REMOVED)
                    self.store.journal(now, "batch-removed", batch["id"], reason="native-evicted")
                    self._clear_admission_state(batch["id"])
                withdrawn = {
                    number: self._withdraw(repo, number, "cancelled", actor)["withdrawn"] for number in sorted(named)
                }
            for batch in affected:
                self._cancel_superseded_runs(batch["id"], now, reason="batch removed (native-evicted)")
            self._wake.set()
            self._refresh_snapshot()
            return {
                "evicted": True,
                "batches": [batch["id"] for batch in affected],
                "returned": returned,
                "withdrawn": withdrawn,
            }

    def retry_ci(
        self, repo: str, actor: str, *, request_id: str | None = None, source_access=None, source_refresh=None
    ) -> dict[str, Any]:
        """Operator: supersede infrastructure failures or an unresolved send before a new dispatch.

        Refused (`reason: live-run`) while GitHub reports a run for the head's tip queued or in
        progress, since the re-dispatch would cancel it; `github-unavailable` when that cannot be checked.
        """
        candidate_ci = self._candidate_coordinator()
        with self._lock:
            if self.source_authority is not None:
                access, _ = self._source_request_access(source_access)
                actor = principal_key(access.actor)
            self._check_repo(repo)
            head = self._head()
            if head is None:
                return {"retried": False, "reason": "no-head"}
            now = self.clock()
            attempts = self._tip_attempts(head)
            current_attempt = self._current_attempt(head)
            if candidate_ci is not None and (
                current_attempt is None or request_id is None or request_id != current_attempt["request_id"]
            ):
                return {"retried": False, "reason": "request-identity-required"}
            if candidate_ci is not None:
                hold_key = f"source-ci:promotion-hold:{current_attempt['id']}"
                hold = self.store.get_setting(hold_key)
                if hold:
                    # Repair a refused promotion without discarding its exact
                    # successful CI request or starting another expensive build.
                    members = self._members(head["id"])
                    try:
                        with self._lock.unlocked():
                            candidate_ci.verify_promotion(current_attempt["id"], members, self.source)
                    except Exception as error:  # noqa: BLE001 - preserve the hold on any failed guard
                        return {"retried": False, "reason": "promotion-refused", "error": type(error).__name__}
                    if self.source_authority is not None:
                        source_access = self._refresh_source_access(source_access, source_refresh)
                    if (
                        self._head() != head
                        or self._current_attempt(head) != current_attempt
                        or self._members(head["id"]) != members
                        or self.store.get_setting(hold_key) != hold
                    ):
                        return {"retried": False, "reason": "head-changed"}
                    with self.store.transaction():
                        self.store.set_setting(hold_key, None)
                        self.store.update(
                            "ci_attempts",
                            {"id": current_attempt["id"]},
                            needs_observation=1,
                            outcome_detail="promotion hold repaired; rechecking exact CI request",
                        )
                        self.store.journal(
                            self.clock(),
                            "retry-source-promotion",
                            head["id"],
                            actor=actor,
                            attempt_id=current_attempt["id"],
                            request_id=request_id,
                        )
                    self._wake.set()
                    return {"retried": True, "promotion_only": True, "request_id": request_id}
                prepared = current_attempt["dispatch_state"] == "prepared"
                if prepared and current_attempt["outcome"] == OUTCOME_PENDING:
                    with self.store.transaction():
                        changed = self.store.update(
                            "ci_attempts",
                            {"id": current_attempt["id"], "dispatch_state": "prepared", "outcome": OUTCOME_PENDING},
                            outcome=OUTCOME_SUPERSEDED,
                        )
                        self.store.journal(
                            now,
                            "retry-ci",
                            head["id"],
                            actor=actor,
                            forgotten=changed,
                            request_id=request_id,
                            prepared=True,
                        )
                    self._wake.set()
                    return {"retried": bool(changed), "forgotten": changed}
            if attempts:
                # A new dispatch would cancel a live run for the tip (pr-build.yml's concurrency group).
                try:
                    for attempt in attempts:
                        if attempt["outcome"] == OUTCOME_PENDING and attempt["dispatch_state"] in {
                            "sending",
                            "uncertain",
                        }:
                            self._observe_attempt(head, dict(attempt, needs_observation=1), now, strict=True)
                    attempts = self._tip_attempts(head)
                    run = self._live_run(head, attempts)
                except ControllerError as error:
                    if error.code == "head-changed":
                        return {"retried": False, "reason": "head-changed"}
                    raise
                except Exception as error:  # noqa: BLE001
                    return {"retried": False, "reason": "github-unavailable", "detail": str(error)}
                if self.source_authority is not None:
                    source_access = self._refresh_source_access(source_access, source_refresh)
                    now = self.clock()
                    if not self._source_head_current(head) or self._tip_attempts(head) != attempts:
                        return {"retried": False, "reason": "head-changed"}
                if run is not None:
                    self.store.journal(now, "retry-ci-refused", head["id"], actor=actor, run_id=run.run_id)
                    return {
                        "retried": False,
                        "reason": "live-run",
                        "detail": f"run {run.run_id} for the tip is {run.status}; a re-dispatch would cancel it",
                        "run_id": run.run_id,
                        "status": run.status,
                    }
            # An explicit operator retry can also supersede an unresolved send,
            # after the same live-run check. Normal ticks never retransmit it.
            forgotten = []
            with self.store.transaction():
                for attempt in attempts:
                    unknown = attempt["outcome"] == OUTCOME_PENDING and attempt["dispatch_state"] in {
                        "sending",
                        "uncertain",
                    }
                    # Native retries name one exact request, and the live-run
                    # check above must prove its owned run has finished. A
                    # completed run without usable evidence can then be retired
                    # explicitly; no absence or title proves an unowned send lost.
                    source_pending = (
                        candidate_ci is not None
                        and attempt["request_id"] == request_id
                        and attempt["outcome"] == OUTCOME_PENDING
                        and attempt["dispatch_state"] == "accepted"
                    )
                    if attempt["outcome"] == OUTCOME_INFRASTRUCTURE or unknown or source_pending:
                        changed = self.store.update(
                            "ci_attempts",
                            {
                                "id": attempt["id"],
                                "outcome": attempt["outcome"],
                                "dispatch_state": attempt["dispatch_state"],
                            },
                            outcome=OUTCOME_SUPERSEDED,
                        )
                        if changed:
                            forgotten.append(
                                {"id": attempt["id"], "request_id": attempt["request_id"], "unknown_outcome": unknown}
                            )
                count = len(forgotten)
                self.store.journal(now, "retry-ci", head["id"], actor=actor, forgotten=count, attempts=forgotten)
            self._wake.set()
            return {"retried": bool(count) if candidate_ci is not None else True, "forgotten": count}

    def _tip_attempts(self, head: dict[str, Any]) -> list[dict[str, Any]]:
        """The attempts at the head's current version for its current tip and base, oldest first."""
        return [
            a
            for a in self.store.rows("ci_attempts", {"batch_id": head["id"], "batch_version": head["version"]})
            if a["head_sha"] == head["tip"] and a["base_sha"] == head["base"]
        ]

    def _source_head_current(self, head, attempt=None):
        current = self._head()
        if current is None or any(current[key] != head[key] for key in ("id", "version", "tip", "base")):
            return False
        if attempt is None:
            return True
        row = self.store.row("ci_attempts", {"id": attempt["id"]})
        return (
            row is not None
            and row["request_id"] == attempt["request_id"]
            and row["outcome"] == OUTCOME_PENDING
            and row["cancelled_at"] is None
        )

    def _live_run(self, head: dict[str, Any], attempts: list[dict[str, Any]]) -> CandidateRun | None:
        """The newest run GitHub reports for the head's tip since its first attempt, when it is still
        queued or in progress; `None` when there is none. Raises when GitHub cannot be asked."""
        candidate_ci = self._candidate_coordinator(batch_id=head["id"])
        if candidate_ci is not None:
            with self._lock.unlocked():
                live = candidate_ci.live_attempt(attempts, discover_completed=True)
            if not self._source_head_current(head):
                raise ControllerError("head-changed", "source head changed during live CI discovery")
            return None if live is None else live[1]
        run = self.execution.find_candidate_run(
            self.repository,
            batch_id=str(head["id"]),
            head_sha=head["tip"],
            base_sha=head["base"],
            since=min(a["dispatched_at"] for a in attempts),
        )
        if run is None or run.status not in LIVE_RUN_STATUSES:
            return None
        # A run the controller cancelled, or is still cancelling (an open cancel intent), may read as
        # live; it is not re-adopted.
        cancelling = {
            str(a["run_id"])
            for a in self.store.rows("ci_attempts", {"batch_id": head["id"]})
            if a["run_id"] and self._cancelling(a)
        }
        return None if run.run_id in self._cancelled_run_ids(head["id"]) | cancelling else run

    def _readopt_live_run(self, head: dict[str, Any], now: float) -> bool:
        """Before any re-dispatch of a tip that was dispatched before: re-adopt a run for it that GitHub
        still reports queued or in progress, instead of dispatching a duplicate beside it.

        A duplicate is not only wasted: pr-build.yml's concurrency group cancels the live staging run
        when the second one starts. So a live run is re-adopted whatever its attempt was classified as
        (lost, no-run, superseded). The newest attempt for the tip becomes pending again, bound to the
        live run, so the staging candidate branch named after the attempt stays the one CI verifies.
        Returns whether the dispatch must not happen this tick (re-adopted, or GitHub could not be asked).
        An attempt with an open cancel intent is never re-adopted: the intent would later cancel the
        re-adopted work (a head the emergency batch evicted, returning unchanged).
        """
        candidate_ci = self._candidate_coordinator(batch_id=head["id"])
        attempts = [a for a in self._tip_attempts(head) if not self._cancelling(a)]
        if not attempts:
            return False
        try:
            qualified = None
            if candidate_ci is not None:
                with self._lock.unlocked():
                    qualified = candidate_ci.live_attempt(attempts)
                if not self._source_head_current(head):
                    return True
            run = (
                (None if qualified is None else qualified[1])
                if candidate_ci is not None
                else self._live_run(head, attempts)
            )
        except Exception as error:  # noqa: BLE001
            self._reasons.append(f"head {head['id']}: cannot check for a live CI run before re-dispatch: {error}")
            return True
        if run is None:
            return False
        attempt = (
            qualified[0]
            if qualified is not None
            else next(
                (
                    a
                    for a in reversed(attempts)
                    if a["dispatch_state"] in {"legacy", "accepted", "sending", "uncertain"}
                ),
                None,
            )
        )
        if attempt is None:
            self._reasons.append(f"head {head['id']}: live run {run.run_id} has no previously sent attempt; waiting")
            return True
        if candidate_ci is not None and candidate_ci.ledger.owner_run(attempt["id"]) != run.run_id:
            self._reasons.append(f"head {head['id']}: verified duplicate run {run.run_id} is still live; waiting")
            return True
        where = {"id": attempt["id"]}
        if candidate_ci is not None:
            where.update(
                {key: attempt[key] for key in ("outcome", "dispatch_state", "cancelled_at", "run_id", "request_id")}
            )
        with self.store.transaction():
            # The selected attempt may have changed while discovery released
            # the controller lock. Never undo an explicit retry/cancellation.
            changed = self.store.update(
                "ci_attempts",
                where,
                outcome=OUTCOME_PENDING,
                outcome_detail="",
                run_id=run.run_id,
                run_status=run.status,
                last_progress_at=now,
                needs_observation=1,
            )
            if changed != 1:
                return True
            # A late live run belongs to a previously sent attempt, including its
            # existing candidate ref. Newer rejected receipts must not hide it.
            for rejected in attempts:
                if rejected["id"] > attempt["id"] and rejected["dispatch_state"] == "rejected":
                    match = {"id": rejected["id"]}
                    if candidate_ci is not None:
                        match.update(dispatch_state="rejected", outcome=rejected["outcome"])
                    self.store.update("ci_attempts", match, outcome=OUTCOME_SUPERSEDED)
            self._count("ci_readoptions")
            self.store.journal(
                now,
                "run-readopted",
                head["id"],
                attempt_id=attempt["id"],
                run_id=run.run_id,
                status=run.status,
                previous_outcome=attempt["outcome"],
                previous_detail=attempt["outcome_detail"],
            )
        self._settle_accepted(head, attempt, now)
        self.log.info(
            "head %s: re-adopted live CI run %s (%s) instead of re-dispatching after %s %s",
            head["id"],
            run.run_id,
            run.status,
            attempt["outcome"],
            attempt["outcome_detail"],
        )
        self._reasons.append(f"head {head['id']} re-adopted live CI run {run.run_id} ({run.status})")
        return True

    def _read_run(self, run_id: str, *, with_jobs: bool) -> CandidateRun | None:
        """`read_run` on the port; a port without it reads nothing directly (raises, so nothing is lost)."""
        candidate_ci = self._candidate_coordinator(run_id=run_id)
        if candidate_ci is not None:
            return candidate_ci.read_run(run_id)
        reader = getattr(self.execution, "read_run", None)
        if reader is None:
            raise ControllerError("no-read-run", "the GitHub port cannot read a run directly")
        return reader(self.repository, run_id, with_jobs=with_jobs)

    def _cancelling(self, attempt: dict[str, Any]) -> bool:
        """The attempt has an open cancel intent (`_settle_cancel_intents`)."""
        return self._state(f"cancel_intent:{attempt['id']}") is not None

    def _cancelled_run_ids(self, batch_id: int) -> set[str]:
        """The runs of the batch's attempts that the controller cancelled itself."""
        return {
            str(a["run_id"])
            for a in self.store.rows("ci_attempts", {"batch_id": batch_id})
            if a["cancelled_at"] is not None and a["run_id"]
        }

    def _cancel_superseded_runs(self, batch_id: int, now: float, *, reason: str, by: int | None = None) -> None:
        """Cancel the run of every attempt of the batch other than `by` that is still live as last observed.

        Called after a new attempt `by` is dispatched, and after the batch lands, is removed or has its
        head rebuilt: an earlier attempt's run would otherwise keep a runner busy verifying a candidate
        nobody will use. A still pending attempt becomes `superseded`; one already classified (an
        infrastructure failure whose run lingers) keeps its outcome, so the retry count stands. A failed
        run lookup or cancel request leaves a durable cancel intent (`_write_cancel_intent`), retried
        with backoff until the run is settled (`_settle_cancel_intents`); it never holds anything back.
        The attempt is recorded as cancelled either way, so observation and the re-adoption of live
        runs leave its run alone.
        """
        adopted = self.store.row("ci_attempts", {"id": by}) if by is not None else None
        protected_run = adopted["run_id"] if adopted is not None else None
        for attempt in self.store.rows("ci_attempts", {"batch_id": batch_id}, order="id"):
            if (
                self.candidate_ci is None
                and self.mixed_ci is None
                and self.store.row("ci_requests", {"attempt_id": attempt["id"]}) is not None
            ):
                continue
            candidate_ci = self._candidate_coordinator(attempt_id=attempt["id"])
            if candidate_ci is None and self.store.row("ci_requests", {"attempt_id": attempt["id"]}) is not None:
                continue  # Disabled source: retain ownership for the enrolled coordinator.
            cancel = (
                candidate_ci.cancel_run if candidate_ci is not None else getattr(self.execution, "cancel_run", None)
            )
            if cancel is None:
                continue
            if attempt["id"] == by or attempt["cancelled_at"] is not None:
                continue
            if protected_run and attempt["run_id"] == protected_run:
                continue
            if (
                not attempt["run_id"]
                and attempt["outcome"] == OUTCOME_PENDING
                and attempt["dispatch_state"] != "legacy"
            ):
                self.store.update(
                    "ci_attempts",
                    {"id": attempt["id"], "outcome": OUTCOME_PENDING},
                    outcome=OUTCOME_SUPERSEDED,
                    outcome_detail=f"ci-superseded: {reason}",
                )
                if attempt["dispatch_state"] in {"sending", "uncertain", "accepted"}:
                    try:
                        if candidate_ci is not None:
                            with self._lock.unlocked():
                                run = candidate_ci.observe(attempt["id"]).run
                        else:
                            run = self.execution.find_candidate_run(
                                self.repository,
                                batch_id=str(batch_id),
                                head_sha=attempt["head_sha"],
                                base_sha=attempt["base_sha"],
                                since=attempt["sent_at"] or attempt["dispatched_at"],
                            )
                        if run is not None:
                            self.store.update(
                                "ci_attempts", {"id": attempt["id"]}, run_id=run.run_id, run_status=run.status
                            )
                            attempt = dict(attempt, run_id=run.run_id, run_status=run.status)
                    except NoOwner as error:
                        # A complete listing found no run. Unless that settles it (an accepted
                        # dispatch past its timeout), its cancel intent looks again after a backoff.
                        if not self._cancel_settled(attempt, now, looked_up=True):
                            self._write_cancel_intent(attempt, reason, now, failed=error)
                    except Exception as error:  # noqa: BLE001 - supersession must not block the queue
                        self.log.warning("superseded dispatch %s lookup failed: %s", attempt["id"], error)
                        self._write_cancel_intent(attempt, reason, now, failed=error)
            if protected_run and attempt["run_id"] == protected_run:
                continue
            if not attempt["run_id"] or attempt["run_status"] == "completed":
                continue
            run_id = str(attempt["run_id"])
            try:
                if candidate_ci is not None:
                    with self._lock.unlocked():
                        result = cancel(run_id)
                else:
                    result = cancel(self.repository, run_id)
                error = None
            except Exception as caught:  # noqa: BLE001 - a failed cancel is retried by its intent, never blocks
                result, error = "failed", caught
            values: dict[str, Any] = {
                "cancelled_at": now,
                "cancelled_by": by,
                "cancel_reason": reason,
                "cancel_result": result if error is None else f"failed: {str(error)[:200]}",
            }
            if attempt["outcome"] == OUTCOME_PENDING:
                values.update(outcome=OUTCOME_SUPERSEDED, outcome_detail=f"ci-cancelled: {reason}")
            with self.store.transaction():
                self.store.update("ci_attempts", {"id": attempt["id"]}, **values)
                self._count("ci_runs_cancelled")
                self.store.journal(
                    now,
                    "ci-cancelled",
                    batch_id,
                    attempt_id=attempt["id"],
                    run_id=run_id,
                    superseded_by=by,
                    reason=reason,
                    result=values["cancel_result"],
                )
                if error is not None:
                    self._write_cancel_intent(attempt, reason, now, failed=error)
            if error is not None:
                self.log.warning(
                    "head %s: cancelling superseded CI run %s failed, retrying through its cancel intent: %s",
                    batch_id,
                    run_id,
                    error,
                )

    def _covering_revision(self, base: str, endpoint: str) -> tuple[str | None, str]:
        """The revision whose published Lake map serves `base`, or None, with what was found.

        Git and HTTP only, called without the controller lock. The candidates are `base` and then its
        first-parent ancestors (at most `BASE_CACHE_ANCESTORS`), newest first, as far as their trees
        differ from the base's only outside the Lake build inputs; staging's cache lookup asks for the
        base exactly and then walks those ancestors, and for such an ancestor every input hash matches."""
        from tauceti_tools.assets.scripts.lake_cache_probe import exact_map_url

        assert self.revision_map_probe is not None
        shown = chain._git(self.clone, "show", f"{base}:lean-toolchain")
        if shown.returncode != 0:
            return None, "the base has no lean-toolchain"
        toolchain = shown.stdout.strip()
        listed = chain._git(self.clone, "rev-list", "--first-parent", f"--max-count={BASE_CACHE_ANCESTORS + 1}", base)
        revisions = listed.stdout.split() if listed.returncode == 0 else [base]
        last_error = ""
        probed = 0
        for revision in revisions:
            if revision != base:
                diff = chain._git(self.clone, "diff", "--name-only", "--no-renames", "-z", revision, base)
                if diff.returncode != 0 or any(lake_build_input(path) for path in diff.stdout.split("\0") if path):
                    break
            probed += 1
            try:
                url = exact_map_url(endpoint, toolchain, revision, self.repository)
                if self.revision_map_probe(url):
                    return revision, url
            except Exception as error:  # noqa: BLE001 - an inconclusive probe is a miss, as in lake_cache_probe
                last_error = f"; last probe failed: {type(error).__name__}: {str(error)[:200]}"
        searched = (
            "itself" if probed <= 1 else f"itself or its {probed - 1} first-parent ancestors with the same build inputs"
        )
        return None, f"no published Lake map for the base {searched}{last_error}"

    def _base_cache_ready(self, head: dict[str, Any], now: float) -> bool:
        """Whether the head may be dispatched on its base: the staging build must never start from a
        `main` revision whose Lake cache is not published. False records the wait (a reason, one
        `base-cache-wait` journal entry per base and, after `base_cache_alert_seconds`, one alert)."""
        endpoint = self.setting("base_cache_endpoint")
        if self.revision_map_probe is None or not endpoint:
            return True
        if self._candidate_coordinator(batch_id=head["id"]) is not None:
            return True  # source-candidate CI reads its own cache scope
        base = head["base"]
        known = self._base_cache.get(base)
        recheck = int(self.setting("base_cache_recheck_seconds"))
        if known is None or (known[0] is None and now - known[1] >= recheck):
            with self._lock.unlocked():
                covering, detail = self._covering_revision(base, endpoint)
            if len(self._base_cache) > 256:
                self._base_cache.clear()
            known = self._base_cache[base] = (covering, now, detail)
            current = self.store.row("batches", {"id": head["id"]})
            if current is None or any(current[key] != head[key] for key in ("state", "version", "tip", "base")):
                return False  # the head moved during the probe; the next pass decides
        covering, _, detail = known
        wait = self._state("base_cache_wait")
        if covering is not None:
            if wait:
                with self.store.transaction():
                    self._set_state("base_cache_wait", None)
                    if wait["base"] == base:
                        self.store.journal(
                            now,
                            "base-cache-published",
                            head["id"],
                            base=base,
                            revision=covering,
                            waited_seconds=now - wait["since"],
                        )
            return True
        if head["urgent"] == URGENT_EMERGENCY:
            with self.store.transaction():
                self.store.journal(
                    now, "dispatch-unpublished-base", head["id"], version=head["version"], base=base, detail=detail
                )
            return True
        if not wait or wait["base"] != base or wait["batch_id"] != head["id"]:
            wait = {"batch_id": head["id"], "base": base, "since": now, "detail": detail, "alerted_at": None}
            with self.store.transaction():
                self._set_state("base_cache_wait", wait)
                self.store.journal(
                    now, "base-cache-wait", head["id"], version=head["version"], base=base, detail=detail
                )
        elif wait["detail"] != detail:
            wait["detail"] = detail
            with self.store.transaction():
                self._set_state("base_cache_wait", wait)
        alert_after = int(self.setting("base_cache_alert_seconds"))
        if alert_after and wait["alerted_at"] is None and now - wait["since"] >= alert_after:
            wait["alerted_at"] = now
            record = {
                "action": "waiting for a published base cache",
                "batch_id": head["id"],
                "version": head["version"],
                "cause": f"no Lake cache is published for base {base} after {now - wait['since']:.0f}s: {detail}",
                "detail": "staging is not dispatched until it is; publish the base's cache (main push CI or by hand)",
            }
            with self.store.transaction():
                self._set_state("base_cache_wait", wait)
                self.store.journal(now, "base-cache-alert", head["id"], base=base, waited_seconds=now - wait["since"])
                self._notify(f"base-cache-wait:{head['id']}:{base}", record)
        self._reasons.append(f"head {head['id']} waiting for base {base} cache to be published ({detail})")
        return False

    def _dispatch(self, head: dict[str, Any], now: float, retry: int) -> None:
        candidate_ci = self._candidate_coordinator(batch_id=head["id"])
        if self._mixed_inert_batch(head):
            self._reasons.append("Forgejo admission is disabled; retaining the mixed batch")
            return
        if not self.setting("publication_enabled"):
            self._reasons.append(
                f"publication disabled: would dispatch CI for head {head['id']} v{head['version']} ({head['tip'][:12]} on {head['base'][:12]})"
            )
            return
        freeze_key = f"source-ci:freeze-error:{head['id']}"
        failed = self.store.get_setting(freeze_key) if candidate_ci is not None else None
        binding = [head["version"], head["tip"], head["base"]]
        if failed and failed["binding"] == binding and now < failed["retry_at"]:
            self._reasons.append(f"head {head['id']}: source CI preparation retry at {failed['retry_at']:.0f}")
            return
        if self._readopt_live_run(head, now):
            return
        if not self._base_cache_ready(head, now):
            return
        # A head from before `batch_tips` existed has its current tip recorded here, so that the
        # dispatch after its next rebase names it.
        self._record_tip(head["id"], head["version"], head["tip"], now)
        repair_outputs_revs = self._repair_outputs_revs(head)
        # Persist intent before source or execution I/O. This row's id names
        # the candidate branch and must survive an accepted response being lost.
        request_id = secrets.token_hex(16)
        members = self._members(head["id"])
        try:
            prepared = None
            if candidate_ci is not None:
                with self._lock.unlocked():
                    prepared = candidate_ci.prepare(head, members, request_id, fetch_missing=self._fetch_head)
                current = self.store.row("batches", {"id": head["id"]})
                if current is None or any(current[key] != head[key] for key in ("state", "version", "tip", "base")):
                    return
                if not self.setting("publication_enabled"):
                    return
            with self.store.transaction():
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
                    repair_outputs_revs=dumps(repair_outputs_revs),
                    request_id=request_id,
                    dispatch_state="prepared",
                )
                if candidate_ci is not None:
                    candidate_ci.freeze(attempt_id, members, prepared=prepared)
        except Exception as error:  # noqa: BLE001 - head failure must not stop unrelated queue work
            if candidate_ci is None:
                raise
            detail = f"{type(error).__name__}: {str(error)[:200]}"
            self._reasons.append(f"head {head['id']}: cannot freeze source CI request ({detail})")
            same = failed and failed["binding"] == binding and failed["detail"] == detail
            count = failed["count"] + 1 if failed and failed["binding"] == binding else 1
            retry_at = now + int(self.setting("infrastructure_retry_backoff_seconds")) * 2 ** min(3, count - 1)
            self.store.set_setting(freeze_key, dict(binding=binding, detail=detail, count=count, retry_at=retry_at))
            if not same:
                self.store.journal(
                    now, "source-ci-freeze-failed", head["id"], error=type(error).__name__, detail=detail
                )
            return
        if candidate_ci is not None and failed:
            self.store.set_setting(freeze_key, None)
        self._send_prepared(head, self.store.row("ci_attempts", {"id": attempt_id}), now)

    def _send_prepared(self, head: dict[str, Any], attempt: dict[str, Any], now: float) -> None:
        """Finish a persisted preparation, then attempt its execution send once."""
        candidate_ci = self._candidate_coordinator(attempt_id=attempt["id"])
        if not self.setting("publication_enabled"):
            self._reasons.append(f"publication disabled: would resume CI for head {head['id']}")
            return
        attempt_id = attempt["id"]
        current = self.store.row("ci_attempts", {"id": attempt_id})
        if current is None or current["outcome"] != OUTCOME_PENDING or current["dispatch_state"] != "prepared":
            return
        if (attempt["head_sha"], attempt["base_sha"], attempt["batch_version"]) != (
            head["tip"],
            head["base"],
            head["version"],
        ):
            self.store.update("ci_attempts", {"id": attempt_id, "outcome": OUTCOME_PENDING}, outcome=OUTCOME_SUPERSEDED)
            return
        repair_outputs_revs = loads(attempt["repair_outputs_revs"], [])
        members = [m["number"] for m in self._members(head["id"])]
        branch = self.staging_candidate_ref(attempt_id, head["version"])
        sending = False
        try:
            # The candidate exists only in the service's clone until it is retained on GitHub; CI checks it
            # out by SHA, so the retained ref is pushed before the dispatch rather than by the outbox later.
            # The staging candidate branch is the same commit under the name pr-build.yml's train identity
            # check fetches and compares with the dispatched head.
            if candidate_ci is not None:
                with self._lock.unlocked():
                    dispatch_prepared = candidate_ci.prepare_source(attempt_id, self.source)
                current_head = self.store.row("batches", {"id": head["id"]})
                if current_head is None or any(
                    current_head[key] != head[key] for key in ("state", "version", "tip", "base")
                ):
                    self.store.update(
                        "ci_attempts",
                        {"id": attempt_id, "dispatch_state": "prepared", "outcome": OUTCOME_PENDING},
                        outcome=OUTCOME_SUPERSEDED,
                    )
                    return
                if not self.setting("publication_enabled"):
                    return
            else:
                retained_ref = self.batch_ref(head["id"], head["version"])
                retained = self.store.row("outbox", {"key": f"retain:{retained_ref}:{head['tip']}"})
                if retained is None or retained["state"] != "done":
                    self.source.update_ref(self.repository, retained_ref, head["tip"])
                    if retained is not None:
                        self._outbox_done(retained, loads(retained["history"], []), "retained-by-dispatch", now)
                self.source.update_ref(self.repository, f"refs/heads/{branch}", head["tip"])
            now = self.clock()
            claimed = self.store.update(
                "ci_attempts",
                {"id": attempt_id, "dispatch_state": "prepared", "outcome": OUTCOME_PENDING},
                dispatch_state="sending",
                dispatch_error="",
                sent_at=now,
                last_progress_at=now,
            )
            if claimed != 1:
                return  # Another recovery process already owns or settled this send.
            sending = True
            if candidate_ci is not None:
                with self._lock.unlocked():
                    dispatch_id = candidate_ci.dispatch(attempt_id, prepared=dispatch_prepared)
            else:
                dispatch_id = self.execution.dispatch_candidate(
                    self.repository,
                    head_sha=head["tip"],
                    base_sha=head["base"],
                    base_ref=self.canonical_branch,
                    batch_id=str(head["id"]),
                    members=members,
                    repair_outputs_revs=repair_outputs_revs,
                )
        except Exception as error:  # noqa: BLE001
            uncertain = sending and not isinstance(error, DispatchNotAccepted)
            with self.store.transaction():
                current = self.store.row("ci_attempts", {"id": attempt_id})
                state = "sending" if sending else "prepared"
                if current is None or current["dispatch_state"] != state:
                    return
                pending = current["outcome"] == OUTCOME_PENDING
                retryable = (
                    getattr(error, "retryable", not isinstance(error, (ValueError, TypeError, KeyError)))
                    if candidate_ci is not None
                    else getattr(error, "retryable", True)
                )
                resume_prepared = candidate_ci is not None and not uncertain and pending and retryable
                retry_at = getattr(error, "retry_at", None)
                failures = current["dispatch_failures"] + 1
                if resume_prepared:
                    backoff = int(self.setting("infrastructure_retry_backoff_seconds")) * 2 ** min(3, failures - 1)
                    retry_at = max(retry_at or 0, self.clock() + backoff)
                changed = self.store.update(
                    "ci_attempts",
                    {
                        "id": attempt_id,
                        "dispatch_state": state,
                        "outcome": current["outcome"],
                    },
                    dispatch_state="prepared" if resume_prepared else "uncertain" if uncertain else "rejected",
                    dispatch_error=type(error).__name__,
                    outcome=(OUTCOME_PENDING if uncertain or resume_prepared else OUTCOME_INFRASTRUCTURE)
                    if pending
                    else current["outcome"],
                    outcome_detail=("dispatch outcome unknown" if uncertain else "dispatch not accepted")
                    if pending
                    else current["outcome_detail"],
                    dispatch_retry_at=retry_at,
                    dispatch_retryable=int(retryable),
                    dispatch_failures=failures,
                    last_observed_at=self.clock(),
                )
                if not changed:
                    return
                if uncertain:
                    self._count("ci_dispatch_uncertain")
                self.store.journal(
                    now,
                    "dispatch-failed",
                    head["id"],
                    attempt_id=attempt_id,
                    request_id=attempt["request_id"],
                    uncertain=uncertain,
                    error=str(error),
                )
            self._reasons.append(f"dispatch of head {head['id']} failed: {error}")
            return
        self._settle_accepted(head, attempt, now, dispatch_id=dispatch_id)

    def _settle_accepted(
        self,
        head: dict[str, Any],
        attempt: dict[str, Any],
        now: float,
        *,
        dispatch_id: str | None = None,
        run_id: str | None = None,
    ) -> None:
        """Record acknowledgement or discovery once, preserving any superseded outcome."""
        attempt_id = attempt["id"]
        with self.store.transaction():
            current = self.store.row("ci_attempts", {"id": attempt_id})
            if current is None or current["dispatch_state"] not in {"sending", "uncertain"}:
                return
            changed = self.store.update(
                "ci_attempts",
                {"id": attempt_id, "dispatch_state": current["dispatch_state"], "outcome": current["outcome"]},
                dispatch_id=dispatch_id or current["dispatch_id"],
                dispatch_state="accepted",
                dispatch_error="",
                run_id=run_id or current["run_id"],
            )
            if not changed:
                return
            if head["first_dispatch_at"] is None:
                self.store.update(
                    "batches",
                    {"id": head["id"], "first_dispatch_at": None},
                    first_dispatch_at=current["sent_at"] or now,
                )
            self._count("ci_dispatches")
            retry = attempt["infrastructure_retry"]
            if retry:
                self._count("ci_redispatches")
            self.store.journal(
                now,
                "dispatched",
                head["id"],
                version=head["version"],
                tip=head["tip"],
                dispatch_id=dispatch_id or current["dispatch_id"],
                attempt_id=attempt_id,
                request_id=attempt["request_id"],
                superseded=current["outcome"] != OUTCOME_PENDING,
                retry=retry,
                repair_outputs_revs=loads(attempt["repair_outputs_revs"], []),
            )
        if current["outcome"] == OUTCOME_PENDING and self.setting("publication_enabled"):
            self._cancel_superseded_runs(head["id"], now, reason=f"superseded by attempt {attempt_id}", by=attempt_id)

    def _retry_status_delivery(self, attempt: dict[str, Any], run: CandidateRun, now: float) -> None:
        """Reuse the durable outbox and existing intent-only workflow, without a retry limit."""
        candidate_ci = self._candidate_coordinator(attempt_id=attempt["id"])
        if candidate_ci is not None:
            return  # native execution receipts use their existing result protocol
        run_attempt = run.attempt or 1
        key = f"status-delivery:{attempt['id']}:{run.run_id}:{run_attempt}"
        existing = self.store.row("outbox", {"key": key})
        if existing is None:
            self._enqueue(
                "retry_status_delivery",
                key,
                {
                    "attempt_id": attempt["id"],
                    "run_id": run.run_id,
                    "run_attempt": run_attempt,
                    "base_ref": attempt["base_ref"],
                },
            )
        elif existing["state"] == "done" and now - (existing["done_at"] or 0) >= LOG_GRACE_SECONDS:
            # A successful dispatch is not successful delivery. If its job died,
            # timed out or lost the reply, retry the same retained original-run
            # intents. Workflow concurrency coalesces these reporting jobs only.
            self.store.update("outbox", {"id": existing["id"]}, state="pending", next_attempt_at=now)

    def _check_lost_run(
        self, head: dict[str, Any], attempt: dict[str, Any], run: CandidateRun, values: dict[str, Any], now: float
    ) -> None:
        """Decide whether an unfinished run is lost; updates `values` with the verdict when it is.

        A queued (or otherwise waiting) run is never lost. An in-progress run is lost only when its
        jobs have reported nothing for `lost_run_seconds` and a direct read of the run still says
        `in_progress` with an `updated_at` at least that old; short of that a long silence is a
        `stall` reason line, never an infrastructure verdict. A run publishing its final oleans
        (`publishing`) is never lost: a publisher that really died ends when GitHub fails its job.
        """
        threshold = int(self.setting("lost_run_seconds"))
        silence = now - values["last_progress_at"]
        if silence < threshold:
            return
        stall = f"stall: head {head['id']} run {run.run_id} is {run.status or 'unknown'} with no job update for {silence:.0f}s"
        if publishing(loads(values.get("phases"), [])):
            self._reasons.append(f"{stall}; it is publishing its final oleans, not treated as lost")
            return
        if run.status != "in_progress":
            self._reasons.append(f"{stall}; a run GitHub reports {run.status or 'unknown'} is not lost")
            return
        try:
            direct = self._read_run(run.run_id, with_jobs=False)
        except Exception as error:  # noqa: BLE001
            self._reasons.append(f"{stall}; the direct read of the run failed ({error}), not treated as lost")
            return
        if direct is None:
            detail = f"lost-run: run {run.run_id} is no longer on GitHub"
        elif direct.status != "in_progress":
            # Finished (or requeued) since the listing: the next observation reads it in full.
            values["needs_observation"] = 1
            self._reasons.append(f"{stall}; GitHub now reports it {direct.status or 'unknown'}")
            return
        elif direct.updated_at is not None and now - direct.updated_at < threshold:
            self._reasons.append(
                f"{stall}; GitHub updated the run {now - direct.updated_at:.0f}s ago, not treated as lost"
            )
            return
        else:
            age = "unknown" if direct.updated_at is None else f"{now - direct.updated_at:.0f}s"
            detail = f"{LOST_IN_PROGRESS} with no job update for {silence:.0f}s and run updated_at {age} old"
        values.update(outcome=OUTCOME_INFRASTRUCTURE, outcome_detail=detail)
        self.store.journal(
            now, "ci-infrastructure", head["id"], attempt_id=attempt["id"], run_id=run.run_id, detail=detail
        )

    def _observe_attempt(
        self, head: dict[str, Any], attempt: dict[str, Any], now: float, *, strict: bool = False
    ) -> None:
        candidate_ci = self._candidate_coordinator(attempt_id=attempt["id"])
        interval = int(self.setting("polling_interval_seconds"))
        due = (
            attempt["needs_observation"]
            or attempt["last_observed_at"] is None
            or now - attempt["last_observed_at"] >= interval
        )
        if not due:
            return
        if candidate_ci is not None:
            self._observe_source_attempt(head, attempt, now, strict=strict)
            return
        try:
            run = self.execution.find_candidate_run(
                self.repository,
                batch_id=str(head["id"]),
                head_sha=attempt["head_sha"],
                base_sha=attempt["base_sha"],
                since=attempt["sent_at"] or attempt["dispatched_at"],
            )
        except Exception as error:  # noqa: BLE001
            self._reasons.append(f"CI observation failed: {error}")
            if strict:
                raise
            return
        if run is not None and run.run_id != attempt["run_id"] and run.run_id in self._cancelled_run_ids(head["id"]):
            # A run the controller cancelled for an earlier attempt never answers for this one.
            run = None
        values: dict[str, Any] = {"last_observed_at": now, "needs_observation": 0}
        if run is None and attempt["run_id"]:
            # The run this attempt was bound to is missing from the listing: read it by id before
            # calling it lost, so a listing that momentarily omits it costs nothing.
            try:
                run = self._read_run(attempt["run_id"], with_jobs=True)
            except Exception as error:  # noqa: BLE001
                self._reasons.append(f"CI observation failed: direct read of run {attempt['run_id']}: {error}")
                if strict:
                    raise
                return
            if run is None:
                values.update(
                    outcome=OUTCOME_INFRASTRUCTURE,
                    outcome_detail=f"lost-run: run {attempt['run_id']} is no longer on GitHub",
                )
                self.store.journal(
                    now, "ci-infrastructure", head["id"], attempt_id=attempt["id"], detail=values["outcome_detail"]
                )
                self.store.update("ci_attempts", {"id": attempt["id"], "outcome": OUTCOME_PENDING}, **values)
                return
        if run is None:
            # An unknown (`sending`/`uncertain`) dispatch with no run is resent after the same timeout.
            if now - (attempt["sent_at"] or attempt["dispatched_at"]) >= int(self.setting("dispatch_timeout_seconds")):
                values.update(outcome=OUTCOME_INFRASTRUCTURE, outcome_detail="no-run: the dispatch produced no run")
                self.store.journal(
                    now, "ci-infrastructure", head["id"], attempt_id=attempt["id"], detail=values["outcome_detail"]
                )
            self.store.update("ci_attempts", {"id": attempt["id"], "outcome": OUTCOME_PENDING}, **values)
            return
        self._settle_accepted(head, attempt, now, run_id=run.run_id)
        progress = [t for t in (run.created_at, run.started_at, run.completed_at, run.updated_at) if t]
        for phase in run.phases:
            progress.extend(t for t in (phase.started_at, phase.completed_at) if t)
        last_progress = max(progress) if progress else attempt["last_progress_at"]
        phases = [vars(phase) for phase in run.phases]
        # Progress is any change GitHub reports: a phase or step appearing, starting or completing
        # (its timestamps are part of the phase), or the run's `updated_at` moving.
        moved = phases != loads(attempt["phases"], []) or (
            run.updated_at is not None and run.updated_at != attempt["run_updated_at"]
        )
        if moved:
            last_progress = now
        same_run = run.run_id == attempt["run_id"] and run.attempt == attempt["run_attempt"]
        completed_at = attempt["run_completed_at"] if same_run else None
        values.update(
            run_id=run.run_id,
            run_attempt=run.attempt,
            run_status=run.status,
            run_conclusion=run.conclusion,
            run_created_at=run.created_at,
            run_started_at=run.started_at,
            # Freeze the observed completion time for this run/attempt. The adapter may supply
            # GitHub's updated_at, which can move after completion; missing timestamps use now.
            run_completed_at=(completed_at or run.completed_at or now)
            if run.status == "completed"
            else run.completed_at,
            run_updated_at=run.updated_at if run.updated_at is not None else attempt["run_updated_at"],
            runner_name=run.runner_name,
            runner_labels=dumps(list(run.runner_labels)),
            host=run.host,
            phases=dumps(phases),
            html_url=run.html_url,
            last_progress_at=max(last_progress or 0, attempt["last_progress_at"] or 0) or now,
            diagnostics=dumps(list(run.diagnostics)),
        )
        if run.status != "completed":
            self._check_lost_run(head, attempt, run, values, now)
            self.store.update("ci_attempts", {"id": attempt["id"], "outcome": OUTCOME_PENDING}, **values)
            return
        if run.conclusion not in ("success", "failure"):
            values.update(outcome=OUTCOME_INFRASTRUCTURE, outcome_detail=f"run-{run.conclusion or 'unknown'}")
            self.store.journal(
                now, "ci-infrastructure", head["id"], attempt_id=attempt["id"], detail=values["outcome_detail"]
            )
            self.store.update("ci_attempts", {"id": attempt["id"], "outcome": OUTCOME_PENDING}, **values)
            return
        # Every verdict context is read and bound the same way: a status counts only when its
        # `target_url` names the candidate's own run and it is no longer pending.
        statuses = {
            context: self.execution.commit_status(self.repository, attempt["head_sha"], context)
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
            publication_failed = any(_finalizer_finding(d) for d in run.diagnostics)
            if not publication_failed and now - values["run_completed_at"] < LOG_GRACE_SECONDS:
                self._reasons.append(f"head {head['id']}: run {run.run_id} completed, waiting for build status")
                self.store.update("ci_attempts", {"id": attempt["id"], "outcome": OUTCOME_PENDING}, **values)
                return
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
            self.store.update("ci_attempts", {"id": attempt["id"], "outcome": OUTCOME_PENDING}, **values)
            return
        if status.state == "success":
            unbound = [context for context in VERDICT_CONTEXTS if not bound[context]]
            if unbound:
                # Compilation and exact cache publication already succeeded.
                # Preserve this attempt forever while required status delivery
                # recovers; a transport failure must not schedule another build.
                self._reasons.append(
                    f"head {head['id']}: verified run {run.run_id} awaits delivery of {', '.join(unbound)}"
                )
                self.store.update("ci_attempts", {"id": attempt["id"], "outcome": OUTCOME_PENDING}, **values)
                self._retry_status_delivery(attempt, run, now)
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
            # The finalizer exits nonzero after it posts a failed `build` status, so a failed finalizer
            # job only means "the status may be missing". This status is bound to the run, so it was
            # published: the finalizer's finding is no evidence of infrastructure failure here.
            infrastructure = [
                d for d in run.diagnostics if d.get("kind") == "infrastructure" and not _finalizer_finding(d)
            ]
            log_pending = (
                not source
                and infrastructure
                and all(d.get("reason") == "log_unavailable" for d in infrastructure)
                and not status.description.lower().startswith("infrastructure")
                and now - values["run_completed_at"] < LOG_GRACE_SECONDS
            )
            if log_pending:
                # GitHub publishes a job's log a little after the run completes; a failed build whose log
                # cannot be read yet is neither a source nor an infrastructure verdict. Look again.
                self._reasons.append(f"head {head['id']}: run {run.run_id} failed, waiting for its log")
                self.store.update("ci_attempts", {"id": attempt["id"], "outcome": OUTCOME_PENDING}, **values)
                return
            # The finalizer's "did not complete" status says the build produced no verdict at all.
            no_verdict = status.description.lower().startswith("infrastructure") or (
                status.description == INCOMPLETE_BUILD_DESCRIPTION
            )
            if no_verdict or (infrastructure and not source):
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
        self.store.update("ci_attempts", {"id": attempt["id"], "outcome": OUTCOME_PENDING}, **values)

    def _observe_source_attempt(self, head, attempt, now, *, strict=False):
        """Only the artifact verifier may decide a source candidate's pass or source failure.

        Unavailable APIs keep the request pending. A complete listing with no owner after
        `dispatch_timeout_seconds`, a deleted owned run, and a completed owned run with no
        verified result after publication grace are infrastructure outcomes, which resend.
        """
        candidate_ci = self._candidate_coordinator(attempt_id=attempt["id"])
        values = {"last_observed_at": now, "needs_observation": 0}
        hold = self.store.get_setting(f"source-ci:promotion-hold:{attempt['id']}")
        if hold is not None:
            self.store.update("ci_attempts", {"id": attempt["id"], "outcome": OUTCOME_PENDING}, **values)
            self._reasons.append(
                f"head {head['id']}: source promotion requires operator reconciliation ({hold['kind']})"
            )
            return
        try:
            with self._lock.unlocked():
                observation = candidate_ci.observe(attempt["id"])
                progress = self._source_progress(candidate_ci, attempt, observation.run, now)
        except Exception as error:  # noqa: BLE001 - keep pending, including malformed evidence
            if not self._source_head_current(head, attempt):
                if strict:
                    raise ControllerError("head-changed", "source request changed during CI observation") from None
                return
            key = f"source-ci:evidence-error:{attempt['id']}"
            failed = self.store.get_setting(key)
            if failed is None:
                failed = {"since": now}
                self.store.set_setting(key, failed)
            request_row = self.store.row("ci_requests", {"attempt_id": attempt["id"]})
            owned = request_row is not None and request_row["owner_run_id"] is not None
            since = failed["since"] if owned else (attempt["sent_at"] or attempt["dispatched_at"])
            overdue = now - since >= int(self.setting("dispatch_timeout_seconds"))
            resend = None
            if isinstance(error, RunDeleted):
                resend = f"lost-run: {error}; resending"
            elif isinstance(error, NoOwner) and overdue:
                resend = "unowned-run: no ownership proof after dispatch timeout; resending"
            if resend is not None:
                values.update(outcome=OUTCOME_INFRASTRUCTURE, outcome_detail=resend)
                with self.store.transaction():
                    self.store.journal(
                        now, "source-ci-infrastructure", head["id"], attempt_id=attempt["id"], detail=resend
                    )
                    self.store.update("ci_attempts", {"id": attempt["id"], "outcome": OUTCOME_PENDING}, **values)
                    self.store.execute("DELETE FROM settings WHERE key=?", (key,))
                return
            detail = (
                "reconciliation-required: source CI ownership or evidence unavailable"
                if overdue
                else "waiting for verified source CI ownership and evidence"
            )
            values["outcome_detail"] = detail
            self.store.update("ci_attempts", {"id": attempt["id"], "outcome": OUTCOME_PENDING}, **values)
            self._reasons.append(
                f"head {head['id']}: {detail} ({type(error).__name__}, request {attempt['request_id']})"
            )
            if strict:
                raise
            return
        if not self._source_head_current(head, attempt):
            if strict:
                raise ControllerError("head-changed", "source request changed during CI observation")
            return
        with self.store.transaction():
            self.store.execute("DELETE FROM settings WHERE key=?", (f"source-ci:evidence-error:{attempt['id']}",))
        run = observation.run
        self._settle_accepted(head, attempt, now, run_id=run.run_id)
        if (
            not self._source_head_current(head, attempt)
            or self.store.get_setting(f"source-ci:promotion-hold:{attempt['id']}") is not None
        ):
            return
        now = self.clock()
        values["last_observed_at"] = now
        values.update(
            run_id=run.run_id,
            run_attempt=run.attempt,
            run_status=run.status,
            run_conclusion=run.conclusion,
            run_created_at=run.created_at,
            run_started_at=run.started_at,
            run_updated_at=run.updated_at,
            run_completed_at=(
                attempt["run_completed_at"]
                if attempt["run_id"] == run.run_id and attempt["run_attempt"] == run.attempt
                else None
            )
            or (run.completed_at or now if run.status == "completed" else None),
            html_url=run.html_url,
            outcome_detail="waiting for verified source CI result",
        )
        if run.updated_at != attempt["run_updated_at"]:
            values["last_progress_at"] = now
        same_run = attempt["run_id"] == run.run_id and attempt["run_attempt"] == run.attempt
        if progress is not None:
            # Observational only, as on the ordinary path: a phase or step appearing, starting or
            # completing is progress for the stall report. The verdict below never reads them.
            phases = [vars(phase) for phase in progress.phases]
            if phases != (loads(attempt["phases"], []) if same_run else []):
                values["last_progress_at"] = now
            values.update(
                phases=dumps(phases),
                runner_name=progress.runner_name,
                runner_labels=dumps(list(progress.runner_labels)),
                host=progress.host,
            )
        elif not same_run:
            values.update(phases=dumps([]), runner_name=None, runner_labels=dumps([]), host=None)
        if run.status == "completed":
            completion_key = f"source-ci:completion-seen:{attempt['id']}"
            completed = self.store.get_setting(completion_key)
            if completed is None or (completed["run_id"], completed["attempt"]) != (run.run_id, run.attempt):
                completed = {"run_id": run.run_id, "attempt": run.attempt, "at": now}
                self.store.set_setting(completion_key, completed)
            result = observation.result
            if result is None:
                if now - completed["at"] >= LOG_GRACE_SECONDS:
                    # Absent, expired, unreadable or invalid: a completed run that has not
                    # published a verified result by now is resent rather than left waiting.
                    values.update(
                        outcome=OUTCOME_INFRASTRUCTURE,
                        outcome_detail=f"owned-run-{run.conclusion or 'unknown'}: "
                        "no verified result after publication grace; resending",
                    )
                    self.store.journal(
                        now,
                        "source-ci-infrastructure",
                        head["id"],
                        attempt_id=attempt["id"],
                        run_id=run.run_id,
                        run_attempt=run.attempt,
                    )
            else:
                # GitHubEvidence binds this to the immutable request, repository,
                # current run attempt and trusted isolated publisher.
                outcomes = {
                    "passed": OUTCOME_PASS,
                    "source_failure": OUTCOME_SOURCE_FAILURE,
                    "policy_failure": OUTCOME_SOURCE_FAILURE,
                    "infrastructure": OUTCOME_INFRASTRUCTURE,
                }
                outcome = outcomes[result["classification"]]
                values.update(
                    outcome=outcome,
                    outcome_detail="verified source CI: " + result["classification"],
                    verdicts=dumps(
                        {
                            ("bump-guard" if name == "bump_guard" else name): {
                                "state": verdict,
                                "bound": True,
                                "description": "verified source CI",
                                "target_url": run.html_url,
                            }
                            for name, verdict in result["verdicts"].items()
                        }
                    ),
                )
                if outcome == OUTCOME_SOURCE_FAILURE:
                    # Without log findings (a policy verdict has none), say which verdicts failed.
                    failed = ", ".join(
                        f"{name}: {verdict}"
                        for name, verdict in sorted(result["verdicts"].items())
                        if verdict not in ("success", "not_required")
                    )
                    values["diagnostics"] = dumps(
                        list(run.diagnostics)
                        or [
                            {
                                "kind": "status",
                                "module": "",
                                "message": values["outcome_detail"] + (f" ({failed})" if failed else ""),
                                "target_url": run.html_url,
                            }
                        ]
                    )
                with self.store.transaction():
                    verdict_key = f"source-ci:observed-verdict:{attempt['id']}"
                    verdict_identity = [run.run_id, run.attempt, outcome]
                    # Accounting only: every outcome above still requires fresh
                    # evidence. Re-observing a deferred promotion isn't a new CI.
                    if self.store.get_setting(verdict_key) != verdict_identity:
                        self.store.set_setting(verdict_key, verdict_identity)
                        if outcome == OUTCOME_SOURCE_FAILURE:
                            self._count("ci_source_failures")
                        elif outcome == OUTCOME_PASS:
                            self._count("ci_passes")
                        self.store.journal(
                            now,
                            "source-ci-result",
                            head["id"],
                            attempt_id=attempt["id"],
                            run_id=run.run_id,
                            run_attempt=run.attempt,
                            outcome=outcome,
                            request_digest=result["request_digest"],
                        )
        if (
            self._source_head_current(head, attempt)
            and self.store.get_setting(f"source-ci:promotion-hold:{attempt['id']}") is None
        ):
            self.store.update("ci_attempts", {"id": attempt["id"], "outcome": OUTCOME_PENDING}, **values)

    def _source_progress(self, candidate_ci, attempt, run, now):
        """`run` with its GitHub job and step phases when a read is due, else `None`.

        Due once every `SOURCE_PHASES_INTERVAL_SECONDS` while the run is in progress and once when
        it completes, so the extra jobs call stays bounded. A failed read is display context only:
        it is reported as a reason and never changes the observation.
        """
        if run.phases:
            return run  # the execution already read them (a verified source failure's diagnostics)
        if run.status not in ("in_progress", "completed"):
            return None
        key = (attempt["id"], run.run_id, run.attempt)
        last = self._source_phases_read.get(key)
        if last is not None and (
            last[1] == "completed" or (run.status == "in_progress" and now - last[0] < SOURCE_PHASES_INTERVAL_SECONDS)
        ):
            return None
        if len(self._source_phases_read) >= 64:
            self._source_phases_read.clear()
            self._source_phases_unavailable.clear()
        self._source_phases_read[key] = (now, run.status)
        self._source_phases_unavailable.discard(key)
        try:
            return candidate_ci.progress(attempt["id"], run)
        except Exception as error:  # noqa: BLE001 - phases are optional context
            self._source_phases_unavailable.add(key)
            self._reasons.append(
                f"head {attempt['batch_id']}: source CI phases unavailable for run {run.run_id}: {error}"
            )
            return None

    # --- members leaving the head ----------------------------------------------------------------

    def _return_members(self, head: dict[str, Any], members: list[dict[str, Any]], reason: str, now: float) -> None:
        """Take members out of the head (`removed`); an input still inserted waits again at its original age.
        Called inside the caller's transaction."""
        for member in members:
            # The emergency batch's member returns to the slot it was displaced from, when that is live.
            self._emergency_exit(head, member, now)
            self.store.update("members", {"id": member["id"]}, removed=1, removed_reason=reason)
            row = self.store.row("inputs", {"id": member["input_id"]})
            if row is not None and row["state"] == INPUT_INSERTED and row["batch_id"] == head["id"]:
                self.store.update(
                    "inputs",
                    {"id": row["id"]},
                    state=INPUT_WAITING,
                    batch_id=None,
                    passes_waited=0,
                    last_probe=reason,
                    last_probe_tip="",
                    last_probe_base="",
                    probe_conflicts="{}",
                )
            self.store.journal(now, "member-returned", head["id"], number=member["number"], reason=reason)

    # --- automatic retry backoff and operator notifications ----------------------------------------

    def _retry_delay(self, retries: int) -> float:
        """The automatic retry delay after `retries` earlier retries: `automatic_retry_seconds`, doubling
        each time, at most `automatic_retry_max_seconds`."""
        initial = max(60, int(self.setting("automatic_retry_seconds")))  # never a retry every pass
        return min(
            initial * 2 ** min(20, max(0, retries)), max(initial, int(self.setting("automatic_retry_max_seconds")))
        )

    def _infrastructure_backoff(self, head: dict[str, Any], attempt: dict[str, Any], retries: int, now: float) -> bool:
        """Automatic backoff once a head's CI keeps failing on infrastructure: re-dispatch after
        `automatic_retry_seconds`, doubling up to `automatic_retry_max_seconds`, indefinitely. The backoff
        is one episode across head versions (`state:infrastructure_backoff`) until a run completes on
        source (`_infrastructure_recovered`); its start is told to the operator once. True while waiting."""
        state = self._state("infrastructure_backoff")
        if not state:
            state = {"since": now, "batch_id": head["id"], "version": head["version"], "retries": 0}
            self._set_state("infrastructure_backoff", state)
            self._record_escalation(
                "infrastructure-backoff",
                head,
                now,
                cause=f"CI failed on infrastructure {retries} times at version {head['version']}",
                detail=(
                    f"re-dispatching automatically with backoff: first in {self._retry_delay(0) / 60:.0f} minutes, "
                    f"doubling up to every {self._retry_delay(30) / 3600:.1f} hours, until a run completes; "
                    "one more message on recovery"
                ),
                lines=[attempt["outcome_detail"] or ""],
                key=f"infrastructure-backoff:{now}",
            )
        due = (attempt["last_observed_at"] or attempt["dispatched_at"]) + self._retry_delay(state["retries"])
        due = max(due, attempt.get("dispatch_retry_at") or 0)
        if now < due:
            self._reasons.append(
                f"head {head['id']}: infrastructure backoff since {state['since']:.0f} "
                f"({state['retries']} retries); re-dispatch at {due:.0f}"
            )
            return True
        state["retries"] += 1
        state["last_dispatch_at"] = now
        self._set_state("infrastructure_backoff", state)
        self._count("infrastructure_backoff_retries")
        return False

    def _infrastructure_recovered(self, head: dict[str, Any], attempt: dict[str, Any], now: float) -> None:
        """End an infrastructure backoff once a run completes on source (a pass or a source failure),
        and tell the operator once."""
        state = self._state("infrastructure_backoff")
        if not state:
            return
        self._set_state("infrastructure_backoff", None)
        self._record_escalation(
            "infrastructure-recovered",
            head,
            now,
            cause=f"CI run {attempt['run_id'] or attempt['id']} completed ({attempt['outcome']})",
            detail=(
                f"the infrastructure backoff that began at {state['since']:.0f} ended after "
                f"{state['retries']} automatic retries over {(now - state['since']) / 3600:.1f} hours"
            ),
            key=f"infrastructure-recovered:{state['since']}",
            counted=False,
        )

    def _record_escalation(
        self,
        action: str,
        head: dict[str, Any],
        now: float,
        *,
        cause: str,
        detail: str,
        key: str,
        lines: list[str] | None = None,
        counted: bool = True,
    ) -> None:
        """Keep the escalation for the status page's alerts, journal it, and send it to the operator.
        An uncounted record (a recovery) is journaled and sent but is neither an alert nor an escalation."""
        record = {
            "at": now,
            "action": action,
            "batch_id": head["id"],
            "version": head["version"],
            "cause": cause,
            "detail": detail,
            "lines": (lines or [])[:5],
        }
        with self.store.transaction():
            if counted:
                kept = self._state("escalations", [])
                kept.append(record)
                self._set_state("escalations", kept[-ESCALATIONS_KEPT:])
                self._count("escalations")
            self.store.journal(
                now, "escalated", head["id"], **{k: v for k, v in record.items() if k not in ("at", "batch_id")}
            )
            self._notify(key, record)

    def _notify_stuck_heads(self, now: float) -> None:
        """Queue the one direct message of the current stuck period of the CI recovery report, once due."""
        if not self.stuck_heads_report:
            return
        section = stuck_heads.read(self.stuck_heads_report, now, self.repository)
        found = stuck_heads.notice(section, now, self.public_url)
        if found is None:
            return
        key, content = found
        marker = "tauceti-queue-" + hashlib.sha256(key.encode()).hexdigest()[:24]
        with self.store.transaction():
            if self.store.row("outbox", {"key": f"notify:{key}"}) is not None:
                return
            self.store.journal(
                now,
                "stuck-heads-notified",
                None,
                count=section["count"],
                since=section["stuck_since"],
                prs=[head["pr"] for head in section["heads"]],
            )
            self._enqueue("notify", f"notify:{key}", {"marker": marker, "content": f"{content}\n\nAlert ID: {marker}"})

    def _notify(self, key: str, record: dict[str, Any]) -> None:
        """Queue a direct message to the operator about `record` (outbox kind `notify`, sent once)."""
        marker = "tauceti-queue-" + hashlib.sha256(key.encode()).hexdigest()[:24]
        lines = [
            f"**Tau Ceti merge queue: {record['action']} on head batch {record['batch_id']} "
            f"(version {record['version']})**",
            "",
            f"Why: {escalation.quote(record['cause'], 500)}",
            f"What the queue did: {escalation.quote(record['detail'], 1000)}",
        ]
        if record.get("lines"):
            lines += ["", "```text", *(escalation.quote(line, 300) for line in record["lines"]), "```"]
        if self.public_url:
            lines += ["", f"Status: {self.public_url.rstrip('/')}/status"]
        lines += ["", f"Alert ID: {marker}"]
        self._enqueue("notify", f"notify:{key}", {"marker": marker, "content": "\n".join(lines)})

    # --- promotion and landing ---------------------------------------------------------------------

    def _promote(self, head: dict[str, Any], attempt: dict[str, Any], now: float) -> None:
        candidate_ci = self._candidate_coordinator(attempt_id=attempt["id"])
        if self._mixed_inert_batch(head):
            self._reasons.append("Forgejo admission is disabled; retaining the mixed batch")
            return
        if not self.setting("publication_enabled"):
            self._reasons.append(
                f"publication disabled: would fast-forward {self.canonical_branch} from {head['base'][:12]} to {head['tip'][:12]}"
            )
            return
        key = f"promote:{head['id']}:{head['version']}:{head['tip']}"
        primary_binding = {}
        generation = self.store.get_setting("mixed:primary_generation", 0)
        if generation or self.mixed is not None and self.mixed.mode == "admit":
            key += f":primary:{generation}"
            primary_binding = {
                "primary": self.store.get_setting("mixed:primary", "github"),
                "primary_generation": generation,
            }
        if candidate_ci is not None:
            generation = self.store.get_setting(f"source-ci:promotion-generation:{attempt['id']}", 0)
            key += f":source:{attempt['id']}:{generation}"
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
                        **primary_binding,
                        **({"promotion_send": {"state": "not-sent"}} if primary_binding else {}),
                    },
                )
                self.store.journal(now, "promotion-queued", head["id"], version=head["version"], tip=head["tip"])
        else:
            self._reasons.append(f"head {head['id']} promotion pending in the outbox")

    def _landed(self, batch: dict[str, Any], how: str, now: float) -> None:
        candidate_ci = None if self._inert_ci_batch(batch) else self._candidate_coordinator(batch_id=batch["id"])
        batch = self.store.row("batches", {"id": batch["id"]}) or batch
        if batch["state"] == BATCH_LANDED:
            return
        # Reading every member's landed commit, and diffing each edited one, takes about a second per
        # member: minutes for a large batch. Commits do not change, so that git work runs without the
        # controller lock and before the store transaction; only its use below needs them.
        publish = bool(self.setting("publication_enabled"))
        candidates = [m for m in self._members(batch["id"]) if not self._displaced(batch, m)]
        landed_ranges: dict[str, dict[str, str]] = {}
        if getattr(self.store, "_depth", 0):
            facts = self._landing_facts(batch, candidates, landed_ranges, publish=publish, partial=True)
        else:
            with self._lock.unlocked():
                facts = self._landing_facts(batch, candidates, landed_ranges, publish=publish, partial=True)
            current = self.store.row("batches", {"id": batch["id"]}) or batch
            if current["state"] == BATCH_LANDED:
                return  # another path recorded this landing while the lock was free
            if (current["version"], current["tip"]) != (batch["version"], batch["tip"]):
                # A rebuild or repair replaced the revision the caller saw land: the batch's current
                # revision has not landed, so nothing is recorded; the next canonical observation decides.
                self.store.journal(
                    now, "landing-superseded", batch["id"], tip=batch["tip"], current_tip=current["tip"], how=how
                )
                return
            batch = current
        # A withdrawn member's empty commit lands with the head but the PR does not: it is not marked
        # landed, commented on or closed (`_anticipate` or `_withdraw_member` told its author why). The
        # one exception is a member withdrawn as emptied whose PR has meanwhile landed on `main` by other
        # means: it lands as any externally merged member does.
        everyone = self._members(batch["id"])
        withdrawn = []
        rejected = []
        members = []
        external_members: set[int] = set()
        for member in everyone:
            if self._displaced(batch, member):
                continue  # its input is the emergency batch's, which lands or returns it
            if member["withdrawn"]:
                source = self.store.row("inputs", {"id": member["input_id"]}) or {}
                if source.get("reason") != EMPTY_COMMIT or source.get("state") != INPUT_CANCELLED:
                    withdrawn.append(member["number"])
                    continue
            fact = facts.get(member["id"])
            if fact is None or fact["commit"] != member["commit_sha"]:
                # The member changed while the lock was free: read it now, under the lock.
                fact = self._landing_facts(batch, [member], landed_ranges, publish=publish)[member["id"]]
                facts[member["id"]] = fact
            empty, external = fact["empty"], fact["external"]
            if external:
                external_members.add(member["id"])
            if member["withdrawn"] and not external:
                withdrawn.append(member["number"])
            elif empty and not external:
                rejected.append(member)
            else:
                members.append(member)
        with self.store.transaction():
            # An emptied emergency member returns to its live displaced slot instead of being rejected.
            rejected = [member for member in rejected if not self._emergency_exit(batch, member, now)]
            self.store.update("batches", {"id": batch["id"]}, state=BATCH_LANDED, landed_at=now, landed_how=how)
            if batch["urgent"] == URGENT_EMERGENCY:
                self.store.journal(
                    now, "emergency-landed", batch["id"], tip=batch["tip"], members=[m["number"] for m in members]
                )
            if candidate_ci is not None:
                self.store.execute("DELETE FROM settings WHERE key=?", (f"source-ci:freeze-error:{batch['id']}",))
                for recorded_attempt in self.store.rows("ci_attempts", {"batch_id": batch["id"]}):
                    for category in (
                        "evidence-error",
                        "promotion-hold",
                        "promotion-generation",
                        "observed-verdict",
                        "completion-seen",
                    ):
                        self.store.execute(
                            "DELETE FROM settings WHERE key=?", (f"source-ci:{category}:{recorded_attempt['id']}",)
                        )
            self._clear_admission_state(batch["id"])
            for member in members:
                self.store.update(
                    "inputs",
                    {"id": member["input_id"]},
                    state=INPUT_LANDED,
                    landed_at=now,
                    landed_sha=member["commit_sha"],
                )
            for member in rejected:
                if self.admission is not None:
                    self.admission.withdraw(member["number"], member["accepted_head"])
                self.store.update(
                    "inputs",
                    {"id": member["input_id"]},
                    state=INPUT_CANCELLED,
                    reason=EMPTY_COMMIT,
                    landed_at=None,
                    landed_sha=None,
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
            if rejected:
                self._count("prs_rejected", len(rejected))
            if batch["first_dispatch_at"]:
                cycles = self._state("head_cycle_seconds", [])
                cycles.append(round(now - batch["first_dispatch_at"], 1))
                self._set_state("head_cycle_seconds", cycles[-50:])
            self.store.journal(
                now,
                "landed",
                batch["id"],
                how=how,
                tip=batch["tip"],
                members=[m["number"] for m in members],
                **({"withdrawn": withdrawn} if withdrawn else {}),
                **({"rejected": [m["number"] for m in rejected]} if rejected else {}),
            )
            if self.setting("publication_enabled"):
                for member in members:
                    fact = facts[member["id"]]
                    if publish and fact["diff"] is None and member["provenance"] != "clean":
                        fact.update(self._landing_facts(batch, [member], landed_ranges, publish=True)[member["id"]])
                    self._enqueue_landing_effects(
                        batch, member, landed_externally=member["id"] in external_members, fact=fact
                    )
                for member in rejected:
                    self._enqueue_rejection_effects(batch, member)
            self._interacting_landed({m["number"] for m in members}, now)
            if batch["urgent"] == URGENT_EMERGENCY:
                self._write_cancel_intents(batch, "emergency batch landed", now)
        if self.mixed is not None and self.mixed.mode == "admit" and self.setting("publication_enabled"):
            self._enqueue("mixed-sync", f"mixed-sync:{self.mixed.generation}:{batch['id']}:{batch['tip']}", {})
        if batch["urgent"] == URGENT_EMERGENCY:
            self._settle_cancel_intents(now)
        else:
            self._cancel_superseded_runs(batch["id"], now, reason="batch landed")
        self._wake.set()

    def _landing_facts(
        self,
        batch: dict[str, Any],
        members: list[dict[str, Any]],
        landed_ranges: dict[str, dict[str, str]],
        *,
        publish: bool,
        partial: bool = False,
    ) -> dict[int, dict[str, Any]]:
        """What the clone says about each member's landed commit, keyed by member id: whether it is
        `empty`, whether its PR landed `external`ly, and for an edited member, when its landing is
        published, its `diff` against the accepted head and whether it `outside_queue` landed. Git
        only, on commits that never change, so the caller may run it without the controller lock.
        `partial`: leave out a member git cannot read (one that may turn out not to need reading), for
        the caller to read again under the lock if it does."""
        facts: dict[int, dict[str, Any]] = {}
        for member in members:
            try:
                facts[member["id"]] = self._landing_fact(batch, member, landed_ranges, publish=publish)
            except chain.ChainError:
                if not partial:
                    raise
        return facts

    def _landing_fact(
        self, batch: dict[str, Any], member: dict[str, Any], landed_ranges: dict[str, dict[str, str]], *, publish: bool
    ) -> dict[str, Any]:
        commit = chain.read_commit(self.clone, member["commit_sha"])
        empty = len(commit.parents) == 1 and commit.tree == chain.tree_of(self.clone, commit.parents[0])
        external = empty and chain.carries_landed_note(commit.message)
        if empty and not external:
            since = chain.git(self.clone, "merge-base", member["accepted_head"], batch["base"]).strip()
            if since not in landed_ranges:
                landed_ranges[since] = chain.landed_prs(self.clone, since, batch["base"])
            external = chain.landed_commit(landed_ranges[since], member["source_pr"]) is not None
        edited = publish and member["provenance"] != "clean" and not member["withdrawn"]
        return {
            "commit": member["commit_sha"],
            "empty": empty,
            "external": external,
            "diff": self._landed_diff(member) if edited else None,
            "outside_queue": self._landed_outside_queue(member) if edited else None,
        }

    def _enqueue_rejection_effects(self, batch: dict[str, Any], member: dict[str, Any]) -> None:
        number = member["number"]
        action = "during staging repair of" if member["provenance"] != "clean" else "from staging"
        lines = [
            f"Pull request rejected {action} batch {batch['id']}: no contribution remained.",
            "",
            f"The final staging commit is empty. This PR was not merged into `{self.canonical_branch}`.",
            "",
            f"Rejected revision: `{member['accepted_head']}`.",
        ]
        if member["explanation"].strip():
            lines += ["", "The repair agent's explanation:", ""]
            lines += [f"> {line}" if line else ">" for line in member["explanation"].splitlines()]
        task = self.store.row("tasks", {"id": member["task_id"]}) if member["task_id"] else None
        lines += self._failure_lines(task)
        lines += [
            "",
            f"The staging history is retained under `{self.batch_ref(batch['id'], batch['version'])}`.",
            "",
            f"What to do next: if the contribution is still wanted, rebase it onto `{self.canonical_branch}`, "
            "address the problem described above, and reopen the PR (or open a new one) for approval.",
        ]
        marker = f"<!-- {COMMENT_MARKER} rejected:{self._source_pr(number)}:{batch['id']}:{member['commit_sha']} -->"
        self._enqueue(
            "comment",
            f"rejected-comment:{batch['id']}:{number}",
            {"number": number, "body": "\n".join(lines) + "\n\n" + marker, "marker": marker},
        )
        label = self.setting("membership_label")
        if label:
            self._enqueue(
                "labels",
                f"rejected-labels:{batch['id']}:{number}",
                {"number": number, "add": [], "remove": [label], "input_id": member["input_id"]},
            )
        self._enqueue(
            "close_pr",
            f"rejected-close:{batch['id']}:{number}",
            {"number": number, "rejected": True, "head_sha": member["accepted_head"], "input_id": member["input_id"]},
        )

    def _withdraw_member(
        self,
        batch: dict[str, Any],
        member: dict[str, Any],
        *,
        reason: str,
        comment: str,
        journal_kind: str,
        now: float,
        **detail: Any,
    ) -> None:
        """Send a member back to its author: the PR leaves the batch without landing and stays open.

        The member's empty commit stays in the chain, so the chain keeps its bijection, and the member is
        `withdrawn`: landing neither marks it landed nor closes it, and no agent may give it content again
        (`_no_repair_edits`). Its input is cancelled with `reason`, its approval is withdrawn (a new
        approval brings it back), and the author gets `comment` once through the outbox, keyed by the
        input, with the membership label removed. Called inside the caller's transaction.

        A displaced slot (`_displaced`) and an emergency member whose displaced slot is still live
        (`_emergency_exit`) are only marked withdrawn: the input, its approval and the PR are left alone.
        """
        if self._displaced(batch, member) or self._emergency_exit(batch, member, now):
            self.store.update("members", {"id": member["id"]}, withdrawn=1)
            self.store.journal(
                now,
                journal_kind,
                batch["id"],
                number=member["number"],
                member_id=member["id"],
                reason=reason,
                slot_only=True,
            )
            return
        self.store.update("members", {"id": member["id"]}, withdrawn=1)
        self.store.update(
            "inputs",
            {"id": member["input_id"]},
            state=INPUT_CANCELLED,
            reason=reason,
            landed_at=None,
            landed_sha=None,
        )
        if self.admission is not None:
            self.admission.withdraw(member["number"], member["accepted_head"])
        self.store.journal(
            now,
            journal_kind,
            batch["id"],
            number=member["number"],
            member_id=member["id"],
            commit=member["commit_sha"],
            reason=reason,
            **detail,
        )
        if not self.setting("publication_enabled"):
            return
        number = member["number"]
        marker = f"<!-- {COMMENT_MARKER} withdrawn:{self._source_pr(number)}:{member['input_id']} -->"
        self._enqueue(
            "comment",
            f"withdrawn-comment:{member['input_id']}",
            {"number": number, "body": comment + "\n\n" + marker, "marker": marker},
        )
        label = self.setting("membership_label")
        if label:
            self._enqueue(
                "labels",
                f"withdrawn-labels:{member['input_id']}",
                {"number": number, "add": [], "remove": [label], "input_id": member["input_id"]},
            )

    def _failure_lines(self, task: dict[str, Any] | None) -> list[str]:
        """What the task that changed a member was given to fix: the CI findings (at most
        `COMMENT_FINDINGS`) and the run, or the rebase conflict, as comment lines."""
        if task is None:
            return []
        findings = [
            d
            for d in loads(task["diagnostics"], [])
            if isinstance(d, dict) and d.get("kind") not in ("admission_predictions", "cross_batch", "prepare_errors")
        ]
        attempt = self.store.row("ci_attempts", {"id": task["ci_attempt_id"]}) if task["ci_attempt_id"] else None
        lines = [
            "",
            f"What failed ({task['kind'].replace('_', ' ')} task {task['id']}, batch version {task['batch_version']}):",
        ]
        if attempt is not None:
            run = (
                f"[run {attempt['run_id']}]({attempt['html_url']})"
                if attempt["html_url"]
                else f"run {attempt['run_id']}"
            )
            lines.append(f"- CI {run}: {attempt['outcome_detail'] or attempt['outcome']}")
        for finding in findings[:COMMENT_FINDINGS]:
            if finding.get("kind") == "conflict":
                paths = ", ".join(f"`{path}`" for path in finding.get("paths") or []) or "unknown paths"
                lines.append(f"- rebase conflict in {paths} replaying onto `{str(finding.get('onto') or '')[:12]}`")
                continue
            where = f" `{finding['module']}`" if finding.get("module") else ""
            message = " ".join(str(finding.get("message") or "").split())[:300]
            lines.append(f"- {finding.get('kind') or 'finding'}{where}: {message}")
        if len(findings) > COMMENT_FINDINGS:
            lines.append(f"- and {len(findings) - COMMENT_FINDINGS} more findings")
        if attempt is None and not findings:
            lines.append("- no findings were recorded")
        return lines

    def _emptied_comment(
        self, batch: dict[str, Any], member: dict[str, Any], task: dict[str, Any], explanation: str
    ) -> str:
        """The withdrawal notice of a member an adopted repair reduced to an empty commit."""
        lines = [
            f"The merge queue withdrew this PR from batch {batch['id']}: while repairing the batch, the repair agent "
            "could not make this contribution pass and reduced it to an empty commit. The PR was not merged and "
            "stays open; its approval was withdrawn.",
        ]
        lines += self._failure_lines(task)
        lines += ["", f"Accepted revision: `{member['accepted_head']}`."]
        if explanation:
            lines += ["", "The repair agent's explanation:", ""]
            lines += [f"> {line}" if line else ">" for line in explanation.splitlines()]
        lines += [
            "",
            f"What to do next: rebase onto `{self.canonical_branch}`, address the problem described above, push, "
            "and approve the PR again. A new approval brings it back into the queue.",
        ]
        return "\n".join(lines)

    def _enqueue_landing_effects(
        self,
        batch: dict[str, Any],
        member: dict[str, Any],
        *,
        landed_externally: bool = False,
        fact: dict[str, Any] | None = None,
    ) -> None:
        """`fact`: the member's `_landing_facts`, so the git work behind them is not repeated here."""
        number = member["number"]
        diff = (fact or {}).get("diff")
        if diff is None and member["provenance"] != "clean":
            diff = self._landed_diff(member)
        body = self.member_comment(batch, member, diff=diff)
        if member["provenance"] != "clean":
            self._count("edit_summaries")
            self.store.journal(
                self.clock(),
                "edit-summary",
                batch["id"],
                number=number,
                commit=member["commit_sha"],
                accepted_head=member["accepted_head"],
                diff_lines=diff["lines"],
                truncated=diff["truncated"],
                error=diff["error"],
            )
            label = self.setting("repaired_label")
            outside = (fact or {}).get("outside_queue")
            if outside is None:
                outside = self._landed_outside_queue(member)
            if label and not landed_externally and not outside:
                self._enqueue(
                    "labels",
                    f"repaired-label:{batch['id']}:{number}",
                    {
                        "number": number,
                        "add": [label],
                        "remove": [],
                        "ensure": [
                            {"name": label, "color": REPAIRED_LABEL_COLOR, "description": REPAIRED_LABEL_DESCRIPTION}
                        ],
                    },
                )
        marker = f"<!-- {COMMENT_MARKER} landed:{self._source_pr(number)}:{batch['id']}:{member['commit_sha']} -->"
        self._enqueue(
            "comment",
            f"landed-comment:{batch['id']}:{number}",
            {"number": number, "body": body + "\n\n" + marker, "marker": marker},
        )
        self._enqueue("close_pr", f"landed-close:{batch['id']}:{number}", {"number": number})

    def _landed_outside_queue(self, member: dict[str, Any]) -> bool:
        """Whether the member's landed commit is the empty one carrying the landing note: its PR was
        already on `main`, so the queue emptied it rather than repairing it."""
        try:
            commit = chain.read_commit(self.clone, member["commit_sha"])
            return chain.carries_landed_note(commit.message) and self._commit_empty(member["commit_sha"])
        except chain.ChainError:
            return False

    def member_comment(self, batch: dict[str, Any], member: dict[str, Any], *, diff: dict | None = None) -> str:
        """The one consolidated landing comment for a member: Bors's notice, plus the agent's explanation when edited.
        `diff` is the member's `_landed_diff` when the caller already has it."""
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
        if member["provenance"] != "clean":
            commit_url = f"https://github.com/{self.repository}/commit/{member['commit_sha']}"
            lines += [
                "",
                f"The merge queue edited this contribution while integrating batch {batch['id']}; it landed as [`{member['commit_sha'][:12]}`]({commit_url}).",
                "",
            ]
            lines += [f"> {line}" if line else ">" for line in member["explanation"].splitlines()]
            lines += self._diff_lines(member, commit_url, diff=diff)
            edited = [sha for sha in loads(member["edited_shas"], []) if sha and sha != member["commit_sha"]]
            if edited:
                lines += [
                    "",
                    "Edited revisions: "
                    + ", ".join(f"[`{sha[:12]}`](https://github.com/{self.repository}/commit/{sha})" for sha in edited)
                    + ".",
                ]
            mentioned = self._mentioned_prs(
                member["explanation"],
                exclude=member["number"],
                qualified=any(isinstance(m["number"], SourceNumber) for m in self._members(batch["id"])),
            )
            if mentioned:
                lines += [
                    "",
                    "Related PRs: " + ", ".join(f"[#{n}]({self._pull_url(n)})" for n in mentioned) + ".",
                ]
            lines += ["", f"The source is retained under `{self.batch_ref(batch['id'], batch['version'])}`."]
        return "\n".join(lines)

    def _landed_diff(self, member: dict[str, Any]) -> dict[str, Any]:
        """`git diff <accepted head> <landed commit>` restricted to the PR's own files: those its branch
        changes (since its merge base with the landed commit) and those its landed commit changes.
        `text` is at most `EDIT_DIFF_LINES` lines; `lines` the full count; `error` when git could not say."""
        accepted, landed = member["accepted_head"], member["commit_sha"]
        try:
            base = chain.git(self.clone, "merge-base", accepted, landed).strip()
            files = set(chain.git(self.clone, "diff", "--name-only", "-z", base, accepted).split("\0"))
            files |= set(chain.git(self.clone, "diff", "--name-only", "-z", f"{landed}^", landed).split("\0"))
            files.discard("")
            text = chain.git(self.clone, "diff", accepted, landed, "--", *sorted(files)) if files else ""
        except chain.ChainError as error:
            return {"text": "", "lines": 0, "truncated": False, "error": str(error)[:200]}
        lines = text.splitlines()
        return {
            "text": "\n".join(lines[:EDIT_DIFF_LINES]),
            "lines": len(lines),
            "truncated": len(lines) > EDIT_DIFF_LINES,
            "error": None,
        }

    def _diff_lines(self, member: dict[str, Any], commit_url: str, *, diff: dict | None = None) -> list[str]:
        """The landing comment's diff of the landed commit against the accepted head, in a collapsed block."""
        diff = self._landed_diff(member) if diff is None else diff
        if diff["error"] is not None:
            return [
                "",
                f"The diff against the accepted head is not available here; see [the landed commit]({commit_url}).",
            ]
        if not diff["lines"]:
            return ["", "The landed commit does not differ from the accepted head in this PR's files."]
        fence = "`" * max(3, 1 + max((len(run) for run in re.findall(r"`+", diff["text"])), default=0))
        lines = [
            "",
            "<details><summary>Diff of the landed commit against the accepted head, in this PR's files "
            f"({diff['lines']} lines)</summary>",
            "",
            f"{fence}diff",
            diff["text"],
            fence,
            "",
            "</details>",
        ]
        if diff["truncated"]:
            lines += [
                "",
                f"The diff is {diff['lines']} lines; the first {EDIT_DIFF_LINES} are shown. "
                f"The whole change is [the landed commit]({commit_url}).",
            ]
        return lines

    def _mentioned_prs(self, text: str, exclude: int, *, qualified=False) -> list[int]:
        from .mixed_identity import from_source_pr

        candidates = []
        references = list(_SOURCE_PR_MENTION_RE.finditer(text))
        for match in references:
            value = match.group()
            if not value.startswith("forgejo:") and value.rsplit("#", 1)[0] not in {
                self.provenance_repository,
                self.repository,
            }:
                continue
            try:
                number = from_source_pr(value)
            except (ValueError, TypeError):
                continue
            candidates.append((match.start(), number))
        if not (qualified or isinstance(exclude, SourceNumber)):
            for match in _PR_MENTION_RE.finditer(text):
                if any(reference.start() <= match.start() < reference.end() for reference in references):
                    continue
                if match["repo"] is None or match["repo"] == self.repository:
                    candidates.append((match.start(), int(match["number"])))
        found: list[int] = []
        for _, number in sorted(candidates, key=lambda candidate: candidate[0]):
            if number != exclude and number not in found:
                found.append(number)
        return found

    # --- the no-repair opt-out: problem-anticipated withdrawals ----------------------------------

    def _attribution(self, explanation: str, exclude: int, *, qualified=False) -> dict[str, Any]:
        """The interacting PR, file and finding the agent appended to an emptied `no_repair` member's
        message (`Interacting-PR:`, `Interacting-File:`, `Finding:` lines); the PR falls back to the
        first other PR the text mentions. Missing parts are None."""
        number = None
        match = _INTERACTING_PR_RE.search(explanation)
        if match:
            mentioned = self._mentioned_prs(match["reference"], exclude, qualified=qualified)
            number = mentioned[0] if mentioned else None
        if number is None:
            mentioned = self._mentioned_prs(explanation, exclude=exclude, qualified=qualified)
            number = mentioned[0] if mentioned else None
        file_match = _INTERACTING_FILE_RE.search(explanation)
        finding_match = _FINDING_RE.search(explanation)
        return {
            "interacting": number,
            "file": file_match["file"].strip() if file_match else None,
            "finding": finding_match["finding"] if finding_match else None,
        }

    def _on_canonical(self, number: int) -> bool:
        """Whether a non-empty commit on the canonical branch names PR `number` (` (#N)` subject or `Source-PR`)."""
        if self._canonical is None:
            return False
        # Git uses POSIX ERE, whose literal escaping differs from Python re.escape.
        repository = "".join(
            "\\" + char if char in ".[](){}*+?^$|\\" else char for char in self._member_repository(number)
        )
        pattern = rf"(\(#{int(number)}\)$|^{chain.SOURCE_PR_TRAILER}: {repository}#{int(number)}$)"
        if self.native_source or isinstance(number, SourceNumber):
            pattern = rf"^{chain.SOURCE_PR_TRAILER}: {repository}#{int(number)}$"
        result = chain._git(
            self.clone, "log", "-n", "5", "--format=%H", "-E", f"--grep={pattern}", self._canonical, "--"
        )
        if result.returncode != 0:
            return False
        return any(not self._commit_empty(sha) for sha in result.stdout.split())

    def _interacting_state(self, number: int) -> str:
        """`landed` (on `main`), `queued` (an input waiting or a member), `gone` (left the queue without
        landing) or `unknown` (never queued and not on `main`: followed through canonical advances)."""
        row = self._input_for(number)
        if row is None:
            return "landed" if self._on_canonical(number) else "unknown"
        if row["state"] == INPUT_LANDED or (
            row["state"] == INPUT_CANCELLED and row["reason"] in ("landed-externally", "merged-externally")
        ):
            return "landed"
        if row["state"] in (INPUT_WAITING, INPUT_RESERVED, INPUT_INSERTED):
            return "queued"
        return "gone"

    def _anticipate(self, batch: dict[str, Any], member: dict[str, Any], explanation: str, now: float) -> None:
        """Withdraw a `no_repair` member the agent reduced to an empty commit (`problem-anticipated`).

        The member stays in the chain as that empty commit, so the chain keeps its bijection, but is
        `withdrawn`: when the head lands it is not marked landed, commented on or closed. Its input is
        cancelled with the interacting PR, file and finding parsed from the agent's message, and the
        author is told the problem is not yet reproducible on their branch. The approval is not reused:
        the PR comes back through `needs-rebase` and re-approval once the other side lands, or is
        re-admitted at its original age if the other side leaves the queue without landing (`_follow_anticipated`).
        A displaced slot, or an emergency member returned to its live displaced slot, is only marked withdrawn.
        """
        if self._displaced(batch, member) or self._emergency_exit(batch, member, now):
            self.store.update("members", {"id": member["id"]}, withdrawn=1)
            self.store.journal(now, PROBLEM_ANTICIPATED, batch["id"], number=member["number"], slot_only=True)
            return
        attribution = self._attribution(
            explanation,
            member["number"],
            qualified=any(isinstance(m["number"], SourceNumber) for m in self._members(batch["id"])),
        )
        interacting = attribution["interacting"]
        record = {
            "batch_id": batch["id"],
            "member_id": member["id"],
            "commit": member["commit_sha"],
            **attribution,
            "explanation": explanation,
            "stage": ANTICIPATED_PENDING if interacting is not None else "unattributed",
            "withdrawn_at": now,
        }
        self.store.update("members", {"id": member["id"]}, withdrawn=1)
        self.store.update(
            "inputs",
            {"id": member["input_id"]},
            state=INPUT_CANCELLED,
            reason=PROBLEM_ANTICIPATED,
            anticipated=dumps(record),
        )
        self._count("no_repair_withdrawals")
        self.store.journal(
            now,
            PROBLEM_ANTICIPATED,
            batch["id"],
            number=member["number"],
            commit=member["commit_sha"],
            attributed=interacting is not None,
            **attribution,
        )
        landed = interacting is not None and self._interacting_state(interacting) == "landed"
        if self.setting("publication_enabled"):
            self._enqueue(
                "comment",
                f"anticipated:{member['input_id']}",
                {
                    "number": member["number"],
                    "body": self._anticipated_comment(batch, member, record, landed),
                    "marker": f"<!-- {COMMENT_MARKER} anticipated:{member['input_id']} -->",
                },
            )
        if landed:
            row = self.store.row("inputs", {"id": member["input_id"]})
            assert row is not None
            self._needs_rebase(row, record, now, comment=False)

    def _anticipated_lines(self, record: dict[str, Any]) -> list[str]:
        interacting = record.get("interacting")
        if interacting is None:
            return [
                "The queue could not attribute the change to another PR, file or finding; the agent's note is quoted below."
            ]
        lines = [f"- Interacting PR: #{interacting}"]
        lines.append(f"- File: `{record['file']}`" if record.get("file") else "- File: not named")
        lines.append(f"- Finding: {record['finding']}" if record.get("finding") else "- Finding: not named")
        return lines

    def _anticipated_comment(
        self, batch: dict[str, Any], member: dict[str, Any], record: dict[str, Any], landed: bool
    ) -> str:
        labels = ", ".join(f"`{label}`" for label in self.setting("no_repair_labels")) or "`dont-repair-in-queue`"
        interacting = record.get("interacting")
        row = self.store.row("inputs", {"id": member["input_id"]}) or {}
        why = (
            "This PR changes control files outside the native path policy, which the merge queue never edits"
            if self._control_input(row)
            else f"This PR carries {labels}, so the merge queue does not edit it"
        )
        lines = [
            f"{why}. Integrating it in batch {batch['id']} "
            "needed a change to it, so the queue withdrew it from the batch instead: its commit there is empty and "
            "the PR will not be closed as merged.",
            "",
            *self._anticipated_lines(record),
            "",
        ]
        if interacting is None:
            lines.append(f"Rebase onto `{self.canonical_branch}` and approve it again when ready.")
        elif landed:
            lines.append(
                f"#{interacting} is already on `{self.canonical_branch}`, so the problem is reproducible on your "
                f"branch now: the queue applies `{NEEDS_REBASE_LABEL}`. Rebase, fix it and approve again."
            )
        else:
            lines.append(
                f"You cannot reproduce this on your branch yet: #{interacting} is still in staging, not on "
                f"`{self.canonical_branch}`. When it lands the queue comments again and applies `{NEEDS_REBASE_LABEL}`; "
                f"if it leaves the queue without landing, this PR is re-admitted at its original age."
            )
        if record.get("explanation"):
            lines += ["", "The agent's note:", ""]
            lines += [f"> {line}" if line else ">" for line in record["explanation"].splitlines()]
        return "\n".join(lines)

    def _anticipated_rows(self) -> list[dict[str, Any]]:
        rows = self.store.rows(
            "inputs",
            {"repository": self.repository, "state": INPUT_CANCELLED, "reason": PROBLEM_ANTICIPATED},
            order="id",
        )
        return [
            row
            for row in rows
            if loads(row["anticipated"], {}).get("stage") == ANTICIPATED_PENDING
            and (not isinstance(row["number"], SourceNumber) or self.mixed is not None and self.mixed.mode == "admit")
        ]

    def _needs_rebase(self, row: dict[str, Any], record: dict[str, Any], now: float, *, comment: bool = True) -> None:
        """The interacting PR is on `main`: the problem is reproducible on the author's branch. Comment
        (unless the withdrawal comment already said so), apply `needs-rebase`; the PR re-enters through
        ordinary re-approval."""
        record = dict(record, stage=NEEDS_REBASE_LABEL, converted_at=now)
        self.store.update("inputs", {"id": row["id"]}, anticipated=dumps(record))
        self._count("problem_anticipated_needs_rebase")
        self.store.journal(
            now, NEEDS_REBASE_LABEL, record.get("batch_id"), number=row["number"], interacting=record["interacting"]
        )
        if not self.setting("publication_enabled"):
            return
        if comment:
            body = "\n".join(
                [
                    f"#{record['interacting']} has landed on `{self.canonical_branch}`, so the problem the merge queue "
                    "anticipated for this PR is now reproducible on your branch:",
                    "",
                    *self._anticipated_lines(record),
                    "",
                    f"The queue applied `{NEEDS_REBASE_LABEL}`. Rebase onto `{self.canonical_branch}`, fix it and "
                    "approve again.",
                ]
            )
            self._enqueue(
                "comment",
                f"needs-rebase:{row['id']}",
                {
                    "number": row["number"],
                    "body": body,
                    "marker": f"<!-- {COMMENT_MARKER} needs-rebase:{row['id']} -->",
                },
            )
        self._enqueue(
            "labels",
            f"needs-rebase-label:{row['id']}",
            {"number": row["number"], "add": [NEEDS_REBASE_LABEL], "remove": []},
        )

    def _readmit(self, row: dict[str, Any], record: dict[str, Any], now: float) -> None:
        """The interacting PR left the queue without landing: the withdrawn input waits again at its
        original age, when its PR is still open at the accepted head; otherwise it needs a new approval."""
        pull, reason = self.validate_pull(self.repository, row["number"], row["head_sha"])
        if reason is not None and reason.startswith("fetch-failed"):
            self._note_reason(f"re-admission of #{row['number']} waits for GitHub: {reason}")
            return
        if reason is None:
            blocked = self._blocking_labels(pull)
            reason = f"blocked-label: {', '.join(blocked)}" if blocked else None
        interacting = record["interacting"]
        if reason is not None:
            record = dict(record, stage="readmit-refused", readmit_reason=reason, converted_at=now)
            self.store.update("inputs", {"id": row["id"]}, anticipated=dumps(record))
            self.store.journal(
                now,
                "readmit-refused",
                record.get("batch_id"),
                number=row["number"],
                interacting=interacting,
                reason=reason,
            )
            body = (
                f"#{interacting} left the merge queue without landing, so the problem the queue anticipated for this PR "
                f"no longer applies, but the PR could not be re-admitted as approved ({reason}). Approve it again."
            )
        else:
            record = dict(record, stage="readmitted", converted_at=now)
            self.store.update(
                "inputs",
                {"id": row["id"]},
                state=INPUT_WAITING,
                reason="",
                batch_id=None,
                anticipated=dumps(record),
                no_repair=int(self._carries_no_repair(getattr(pull, "labels", None)) or self._control_input(row)),
                passes_waited=0,
                last_probe="readmitted",
                last_probe_tip="",
                last_probe_base="",
                probe_conflicts="{}",
            )
            self._count("problem_anticipated_readmitted")
            self.store.journal(now, "readmitted", record.get("batch_id"), number=row["number"], interacting=interacting)
            self._wake.set()
            body = (
                f"#{interacting} left the merge queue without landing, so the problem the queue anticipated for this PR "
                "no longer applies: the PR is back in the queue at its original age."
            )
        if self.setting("publication_enabled"):
            self._enqueue(
                "comment",
                f"readmitted:{row['id']}",
                {"number": row["number"], "body": body, "marker": f"<!-- {COMMENT_MARKER} readmitted:{row['id']} -->"},
            )

    def _interacting_landed(self, numbers: set[int], now: float) -> None:
        """PRs seen landing on `main` (a head landing, or a canonical advance naming them): convert the
        withdrawals waiting on them to `needs-rebase`."""
        if not numbers:
            return
        for row in self._anticipated_rows():
            record = loads(row["anticipated"], {})
            if record.get("interacting") in numbers:
                self._needs_rebase(row, record, now)

    def _follow_anticipated(self, now: float) -> None:
        """Each pass: a withdrawal whose interacting PR has landed becomes `needs-rebase`; one whose
        interacting PR left the queue without landing is re-admitted; one whose PR was approved again
        meanwhile is `superseded` (the new approval answers for it)."""
        for row in self._anticipated_rows():
            record = loads(row["anticipated"], {})
            current = self._input_for(row["number"])
            if current is not None and current["id"] != row["id"]:
                record = dict(record, stage="superseded", converted_at=now)
                self.store.update("inputs", {"id": row["id"]}, anticipated=dumps(record))
                continue
            state = self._interacting_state(int(record["interacting"]))
            if state == "landed":
                self._needs_rebase(row, record, now)
            elif state == "gone":
                self._readmit(row, record, now)

    # --- outbox ------------------------------------------------------------------------------------

    def _enqueue(self, kind: str, key: str, payload: dict[str, Any]) -> None:
        now = self.clock()
        if self.store.row("outbox", {"key": key}) is not None:
            return
        self.store.insert("outbox", kind=kind, key=key, payload=dumps(payload), next_attempt_at=now, created_at=now)

    def _drain_outbox(self, now: float) -> None:
        """Perform the due entries: several retained refs go in one push, the rest one by one, until
        `outbox_drain_budget_seconds` of wall time is spent; what remains waits for the next tick,
        which the loop is woken to run at once.

        The GitHub calls run without the controller lock: each entry is re-read under the lock, its
        call made with the lock let go (`ControllerLock.unlocked`), and the outcome recorded under the
        lock again, so worker requests are answered between entries however long the drain runs. A
        `promote` entry reads and changes the records, so it alone is performed under the lock."""
        with self._lock:
            started = time.monotonic()
            budget = float(self.setting("outbox_drain_budget_seconds"))
            due = [
                e for e in self.store.rows("outbox", {"state": "pending"}, order="id") if e["next_attempt_at"] <= now
            ]
            control = {"promote", "mixed-sync", "cancel_run", "retry_status_delivery"}
            due.sort(key=lambda entry: (entry["kind"] not in control, entry["id"]))
        performed = 0
        retains = [e for e in due if e["kind"] == "retain_ref"]
        if len(retains) > 1 and hasattr(self.source, "update_refs"):
            performed += 1
            if self._retain_batch(retains, now):
                due = [e for e in due if e["kind"] != "retain_ref"]
        for index, entry in enumerate(due):
            if performed and time.monotonic() - started >= budget:
                with self._lock:
                    self._reasons.append(f"outbox drain paused with {len(due) - index} entries pending (budget)")
                self._wake.set()
                return
            performed += 1
            self._drain_entry(entry, now)

    def _pending_entry(self, entry: dict[str, Any]) -> dict[str, Any] | None:
        """The entry's current row while it is still pending (another path may have performed it)."""
        current = self.store.row("outbox", {"id": entry["id"]})
        return current if current is not None and current["state"] == "pending" else None

    def _drain_entry(self, entry: dict[str, Any], now: float) -> None:
        with self._lock:
            current = self._pending_entry(entry)
            if current is None:
                return
            if current["kind"] in {"promote", "mixed-sync"} and not self.setting("publication_enabled"):
                return
            if current["kind"] == "promote":
                try:
                    payload = loads(current["payload"], {})
                    payload["_outbox_id"] = current["id"]
                    result = self._perform(current["kind"], payload, now)
                except Exception as error:  # noqa: BLE001 - every adapter failure is recorded and retried
                    self._outbox_failed(current, loads(current["history"], []), error, now)
                else:
                    self._outbox_done(current, loads(current["history"], []), result, now)
                return
        error: Exception | None = None
        result = ""
        try:
            with self._lock.unlocked():
                payload = loads(current["payload"], {})
                if current["kind"] == "notify":
                    payload["_retry"] = current["attempts"] > 0
                result = self._perform(current["kind"], payload, now)
        except Exception as caught:  # noqa: BLE001 - every adapter failure is recorded and retried
            error = caught
        with self._lock:
            current = self._pending_entry(entry)
            if current is None:
                return
            if error is not None:
                self._outbox_failed(current, loads(current["history"], []), error, now)
            else:
                self._outbox_done(current, loads(current["history"], []), result, now)

    def _retain_batch(self, entries: list[dict[str, Any]], now: float) -> bool:
        """Push the entries' retained refs in one `git push` (one connection, one object upload) instead of
        one push each, without the controller lock. On failure nothing is recorded: the caller pushes
        them singly, so one bad ref does not block the rest."""
        payloads = [loads(entry["payload"], {}) for entry in entries]
        updates = [(payload["ref"], payload["sha"]) for payload in payloads]
        try:
            with self._lock.unlocked():
                self.source.update_refs(self.repository, updates)
        except Exception as error:  # noqa: BLE001 - the single pushes record their own failures
            self.log.warning("batched push of %s retained refs failed, pushing singly: %s", len(updates), error)
            return False
        with self._lock:
            for entry in entries:
                current = self._pending_entry(entry)
                if current is not None:
                    self._outbox_done(current, loads(current["history"], []), "retained", now)
        return True

    def _outbox_done(self, entry: dict[str, Any], history: list[Any], result: str, now: float) -> None:
        history.append({"at": now, "result": result})
        with self.store.transaction():
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
            int(self.setting("outbox_retry_seconds")) * (2 ** min(20, attempts - 1)),
            int(self.setting("outbox_retry_max_seconds")),
        )
        history.append({"at": now, "error": str(error)[:500]})
        self.store.update(
            "outbox",
            {"id": entry["id"]},
            attempts=attempts,
            history=dumps(history[-20:]),
            next_attempt_at=getattr(error, "retry_at", None) or now + backoff,
        )
        self.log.warning("outbox %s %s failed (attempt %s): %s", entry["kind"], entry["key"], attempts, error)

    def _ensure_labels(self, repo: str, labels: list[dict[str, str]]) -> None:
        """Create each label the repository may lack, once per process: adding a missing label to a PR
        would otherwise create it with GitHub's default colour and no description."""
        for label in labels:
            label_key = (repo, label["name"])
            if label_key in self._labels_ensured:
                continue
            self.source.ensure_label(repo, label["name"], label["color"], label["description"])
            self._labels_ensured.add(label_key)

    def _perform(self, kind: str, payload: dict[str, Any], now: float) -> str:
        candidate_ci = (
            self._candidate_coordinator(attempt_id=payload.get("attempt_id"), batch_id=payload.get("batch_id"))
            if kind == "cancel_run"
            else None
        )
        repo = self.repository
        from .mixed_identity import SourceNumber

        if isinstance(payload.get("number"), SourceNumber):
            repo = payload["number"].source.repository.key
            self._check_repo(repo)
            if not self.setting("publication_enabled"):
                raise ControllerError("publication-disabled", "native publication remains pending")
        if kind == "retain_ref":
            self.source.update_ref(repo, payload["ref"], payload["sha"])
            return "retained"
        if kind == "comment":
            return "comment " + str(
                self.source.post_comment(repo, payload["number"], payload["body"], payload["marker"])
            )
        if kind == "labels":
            if "admission_generation" in payload:
                work = self.store.row("admission_work", {"repository": repo, "number": payload["number"]})
                if work is None or work["generation"] != payload["admission_generation"]:
                    return "stale-admission"
                pull = self.source.fetch_pr(repo, payload["number"])
                if pull.head_sha != payload["head_sha"] or pull.state != "open" or pull.draft:
                    return "stale-head"
                work = self.store.row("admission_work", {"repository": repo, "number": payload["number"]})
                if work["generation"] != payload["admission_generation"]:
                    return "stale-admission"
            elif "input_id" in payload:
                with self._lock:
                    current = self._input_for(payload["number"])
                    if current and current["id"] != payload["input_id"] and current["state"] == INPUT_INSERTED:
                        return "new-member"
            self._ensure_labels(repo, payload.get("ensure", []))
            self.source.set_labels(repo, payload["number"], payload.get("add", []), payload.get("remove", []))
            return "labels"
        if kind == "close_pr":
            pull = self.source.fetch_pr(repo, payload["number"])
            if pull.state == "closed":
                return "already-closed"
            if payload.get("rejected"):
                if pull.head_sha != payload["head_sha"]:
                    return "stale-head"
                with self._lock:
                    current = self._input_for(payload["number"])
                    if (
                        current
                        and current["id"] != payload["input_id"]
                        and current["state"] in (INPUT_WAITING, INPUT_RESERVED, INPUT_INSERTED)
                    ):
                        return "new-approval"
                title = pull.title
            else:
                title = pull.title if pull.title.startswith(MERGED_TITLE_PREFIX) else MERGED_TITLE_PREFIX + pull.title
            self.source.close_pr(repo, payload["number"], title)
            return "closed"
        if kind == "notify":
            if self.notifier is None:
                return "notifier-unconfigured" + (f": {self.notifier_error}" if self.notifier_error else "")
            # A retry first looks for the marker: the earlier POST may have arrived before its reply was lost.
            return self.notifier.send(payload["marker"], payload["content"], reconcile=bool(payload.get("_retry")))
        if kind == "mixed-sync":
            if self.mixed is None or self.mixed.mode != "admit":
                raise ControllerError("forgejo-inert", "main synchronization awaits source enablement")
            if not self.setting("publication_enabled"):
                raise ControllerError("publication-disabled", "main synchronization remains pending")
            from .mixed_landing import synchronize

            return dumps(synchronize(self.mixed))
        if kind == "promote":
            return self._perform_promotion(payload, now)
        if kind == "retry_status_delivery":
            attempt = self.store.row("ci_attempts", {"id": payload["attempt_id"]})
            head = self._head()
            if (
                attempt is None
                or head is None
                or attempt["batch_id"] != head["id"]
                or attempt["batch_version"] != head["version"]
                or attempt["outcome"] != OUTCOME_PENDING
                or attempt["run_id"] != payload["run_id"]
                or (attempt["run_attempt"] or 1) != payload["run_attempt"]
            ):
                return "superseded status delivery"
            self.execution.retry_status_delivery(
                repo, payload["run_id"], payload["run_attempt"], base_ref=payload["base_ref"]
            )
            return "status recovery requested"
        if kind == "cancel_run" and candidate_ci is None and payload.get("attempt_id") is not None:
            if self.store.row("ci_requests", {"attempt_id": payload["attempt_id"]}) is not None:
                raise ControllerError("forgejo-inert", "qualified cancellation awaits its CI coordinator")
        if kind == "cancel_run":
            # Entries enqueued before failed cancels became cancel intents: one more try, then the
            # attempt's durable intent pursues it (`_settle_cancel_intents`).
            try:
                if candidate_ci is not None:
                    return candidate_ci.cancel_run(payload["run_id"])
                return self.execution.cancel_run(repo, payload["run_id"])
            except Exception as error:  # noqa: BLE001
                attempt = (
                    self.store.row("ci_attempts", {"id": payload["attempt_id"]})
                    if payload.get("attempt_id") is not None
                    else None
                )
                if attempt is None or str(attempt["run_id"]) != str(payload["run_id"]):
                    self.log.warning("retry of cancelling CI run %s failed, giving up: %s", payload["run_id"], error)
                    return f"gave-up: {str(error)[:200]}"
                self._write_cancel_intent(attempt, attempt["cancel_reason"] or "superseded", now, failed=error)
                self.log.warning("cancelling CI run %s failed, its cancel intent retries: %s", payload["run_id"], error)
                return f"cancel-intent: {str(error)[:200]}"
        raise ControllerError("unknown-outbox-kind", kind)

    def _promotion_send_state(self, payload, state, *, lease=False, refused=False):
        entry_id = payload.get("_outbox_id")
        if entry_id is None:
            return
        with self.store.transaction():
            entry = self.store.row("outbox", {"id": entry_id})
            intent = loads(entry["payload"], {})
            previous = intent.get("promotion_send", {})
            uncertain = previous.get("state") == "sending" or not previous.get("state") and entry["attempts"]
            if refused and previous.get("unresolved_unleased"):
                return  # A new refusal does not resolve a preceding lost API reply.
            intent["promotion_send"] = {"state": state}
            if lease and (not uncertain or previous.get("lease") is True):
                intent["promotion_send"]["lease"] = True
            elif state == "sending" and uncertain:
                intent["promotion_send"]["unresolved_unleased"] = True
            self.store.update("outbox", {"id": entry_id}, payload=dumps(intent))

    def _perform_promotion(self, payload: dict[str, Any], now: float) -> str:
        candidate_ci = self._candidate_coordinator(
            attempt_id=payload.get("attempt_id"), batch_id=payload.get("batch_id")
        )
        batch = self.store.row("batches", {"id": payload["batch_id"]})
        if batch is None:
            return "no-batch"
        if self._mixed_inert_batch(batch):
            raise ControllerError("forgejo-inert", "mixed promotion awaits source enablement")
        if candidate_ci is None and any(
            isinstance(member["number"], SourceNumber) for member in self._members(batch["id"])
        ):
            raise ControllerError("forgejo-inert", "native members require their qualified candidate CI coordinator")
        primary_generation = self.store.get_setting("mixed:primary_generation", 0)
        if payload.get("primary_generation", 0) != primary_generation:
            return "stale-primary-generation"
        if self.mixed is None and payload.get("primary", "github") != "github":
            raise ControllerError("forgejo-inert", "Forgejo primary requires active configuration")
        if batch["state"] == BATCH_LANDED:
            return "already-landed"
        if batch["version"] != payload["version"] or batch["tip"] != payload["tip"]:
            return "stale: batch moved on"
        # A lost response from an earlier attempt: the branch may already carry the tip.
        canonical = self._fetch_canonical()
        if canonical == payload["tip"] or chain.is_ancestor(self.clone, payload["tip"], canonical):
            self._canonical = canonical
            self._set_state("canonical_sha", canonical)
            self._promotion_send_state(payload, "settled")
            self._landed(batch, "promoted", now)
            return "already-visible"
        if batch["state"] != BATCH_HEAD:
            return "not-head"  # removed (an emergency batch cancelled before its promotion was sent)
        if candidate_ci is not None:
            attempt = self.store.row("ci_attempts", {"id": payload["attempt_id"]})
            if (
                attempt is None
                or attempt["batch_id"] != batch["id"]
                or attempt["batch_version"] != batch["version"]
                or attempt["head_sha"] != batch["tip"]
                or attempt["base_sha"] != payload["expected_parent"]
                or attempt["cancelled_at"] is not None
                or attempt["outcome"] != OUTCOME_PASS
            ):
                generation_key = f"source-ci:promotion-generation:{payload['attempt_id']}"
                self.store.set_setting(generation_key, self.store.get_setting(generation_key, 0) + 1)
                return "stale-source-attempt"
            members = self._members(batch["id"])
            try:
                with self._lock.unlocked():
                    candidate_ci.verify_promotion(attempt["id"], members, self.source)
            except Exception as error:  # noqa: BLE001 - settle this outbox entry; re-observe or reconcile
                permanent = isinstance(error, PromotionRefused)
                detail = (
                    "reconciliation-required: source promotion refused; repair authority or membership and retry the exact request"
                    if permanent
                    else "source promotion deferred; waiting for current verified evidence"
                )
                with self.store.transaction():
                    changed = self.store.update(
                        "ci_attempts",
                        {"id": attempt["id"], "outcome": OUTCOME_PASS},
                        outcome=OUTCOME_PENDING,
                        outcome_detail=detail,
                        needs_observation=0,
                        last_observed_at=now,
                    )
                    if not changed:
                        return "stale-source-attempt"
                    if permanent:
                        self.store.set_setting(
                            f"source-ci:promotion-hold:{attempt['id']}", {"kind": type(error).__name__}
                        )
                    generation_key = f"source-ci:promotion-generation:{attempt['id']}"
                    self.store.set_setting(generation_key, self.store.get_setting(generation_key, 0) + 1)
                    self.store.journal(
                        now,
                        "source-promotion-deferred",
                        batch["id"],
                        attempt_id=attempt["id"],
                        permanent=permanent,
                        error=type(error).__name__,
                    )
                return "source-reconciliation-required" if permanent else "source-evidence-deferred"
            current = self.store.row("batches", {"id": batch["id"]})
            current_attempt = self.store.row("ci_attempts", {"id": attempt["id"]})
            if (
                current != batch
                or current_attempt is None
                or any(
                    current_attempt[key] != attempt[key]
                    for key in ("outcome", "run_id", "run_attempt", "cancelled_at", "request_id")
                )
                or self._members(batch["id"]) != members
                or self._active_claims(batch["id"])
                or not self.setting("publication_enabled")
            ):
                generation_key = f"source-ci:promotion-generation:{attempt['id']}"
                self.store.set_setting(generation_key, self.store.get_setting(generation_key, 0) + 1)
                return "stale-source-work"
        if self.mixed is not None and self.mixed.mode == "admit":
            from .mixed_landing import preflight

            before = (
                self.store.row("batches", {"id": batch["id"]}),
                self._members(batch["id"]),
                self.store.row("ci_attempts", {"id": payload["attempt_id"]}),
            )
            with self._lock.unlocked():
                preflight(self.mixed, payload["expected_parent"], payload["tip"])
            after = (
                self.store.row("batches", {"id": batch["id"]}),
                self._members(batch["id"]),
                self.store.row("ci_attempts", {"id": payload["attempt_id"]}),
            )
            if before != after or self._active_claims(batch["id"]):
                raise ControllerError(
                    "promotion-changed", "work changed during primary preflight; retained for reconciliation"
                )
            if primary_generation != self.store.get_setting("mixed:primary_generation", 0):
                return "stale-primary-generation"
        if not self.setting("publication_enabled"):
            raise ControllerError("publication-disabled", "landing remains pending while publication is disabled")
        leased = (self.mixed is not None and self.mixed.mode == "admit") or bool(primary_generation)
        self._promotion_send_state(payload, "sending", lease=leased)
        fast_forward = self.source.fast_forward
        if leased and (self.mixed is None or self.mixed.mode != "admit"):
            fast_forward = self.source.fast_forward_leased
        result = fast_forward(self.repository, self.canonical_branch, payload["expected_parent"], payload["tip"])
        self._promotion_send_state(payload, "settled", refused=not result.ok)
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
            open_batches = self._open_batches(include_inert=True)
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
            escalations = self._state("escalations", [])
            recent = now - int(settings["escalation_alert_seconds"])
            for record in escalations:
                if record["at"] >= recent:
                    alerts.append(
                        f"escalation ({record['action']}) on batch {record['batch_id']} v{record['version']}: "
                        f"{record['cause']}; {record['detail']}"
                    )
            backoff = self._state("infrastructure_backoff")
            if backoff:
                alerts.append(
                    f"CI infrastructure backoff since {backoff['since']:.0f}: {backoff['retries']} automatic retries"
                )
            loop_termination = {"infrastructure_backoff": backoff or None}
            if self.notifier_error:
                alerts.append(f"escalation notifier unavailable: {self.notifier_error}")
            if any(e["attempts"] and e["kind"] == "notify" for e in pending):
                alerts.append("escalation notification delivery is failing; see outbox history")
            stuck = stuck_heads.read(self.stuck_heads_report, now, self.repository)
            if (stuck_alert := stuck_heads.alert(stuck, now)) is not None:
                alerts.append(stuck_alert)
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
                **(
                    {
                        "mixed_sources": {
                            "mode": self.mixed.mode,
                            "primary": self.mixed.primary,
                            "generation": self.mixed.generation,
                            "github": self.mixed.profile.github.record(),
                            "forgejo": self.mixed.profile.forgejo.record(),
                            "observation": self.store.get_setting("state:forgejo_observation"),
                            "last_sync": self.store.get_setting("mixed:last_sync"),
                        }
                    }
                    if self.mixed is not None
                    else {}
                ),
                "inference_hold": hold,
                "waiting": {
                    "count": len(waiting),
                    "urgent": sum(1 for row in waiting if row["urgent"]),
                    "control": sum(1 for row in waiting if self._control_input(row)),
                    "oldest_approved_at": min(row["approved_at"] for row in waiting) if waiting else None,
                    "oldest_age_seconds": (now - min(row["approved_at"] for row in waiting)) if waiting else None,
                    "max_passes_waited": max((row["passes_waited"] for row in waiting), default=0),
                    "inputs": [self._input_status(row) for row in waiting],
                },
                "open_batch": None
                if open_batch is None
                else self._batch_status(open_batch, now) | {"pre_rebase": self._pre_rebase_status(open_batch)},
                "open_batches": [self._open_batch_summary(batch, now) for batch in open_batches],
                "sealed_successor": next(
                    (self._open_batch_summary(batch, now) for batch in open_batches if batch["sealed"]), None
                ),
                "head": head_status,
                "emergency": self._emergency_status(),
                "external_head": self._external_head_status(),
                "approvals": {
                    "webhook": counters.get("approvals_webhook", 0),
                    "polled": counters.get("approvals_polled", 0),
                    "revoked_by_poll": counters.get("approvals_revoked_by_poll", 0),
                    "last_poll": self._state("approval_poll"),
                },
                "admission": {"source": "eligibility"}
                if self.admission is None
                else {
                    "source": "policy",
                    "policy_digest": self.admission.policy.revision,
                    **self.admission.state.summary(),
                    **(
                        {"evidence": self.admission.evidence_status()}
                        if callable(getattr(self.admission, "evidence_status", None))
                        else {}
                    ),
                    "merge_base_sources": dict(
                        getattr(getattr(self.admission, "adapter", None), "merge_base_sources", None) or {}
                    ),
                    "snapshot_sources": dict(
                        getattr(getattr(self.admission, "adapter", None), "snapshot_sources", None) or {}
                    ),
                },
                "anticipated": [
                    {
                        "number": row["number"],
                        **{
                            key: loads(row["anticipated"], {}).get(key)
                            for key in ("batch_id", "interacting", "file", "finding", "withdrawn_at")
                        },
                    }
                    for row in self._anticipated_rows()
                ],
                "tasks": [self._task_status(task) for task in tasks],
                "outbox": {"pending": len(pending), "entries": [self._outbox_status(e) for e in pending]},
                "reasons": list(self._reasons),
                "alerts": alerts,
                "stuck_heads": stuck,
                "escalations": escalations[-10:],
                "loop_termination": loop_termination,
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

    def _public_pull_identity(self, number):
        if isinstance(number, SourceNumber):
            return {"source_identity": number.source.record(), "source_pr": number.source_pr}
        if self.mixed is not None:
            return {"source_identity": self.mixed.profile.github.record(), "source_pr": self._source_pr(number)}
        return {}

    def _input_status(self, row: dict[str, Any]) -> dict[str, Any]:
        return {
            "number": int(row["number"]),
            **self._public_pull_identity(row["number"]),
            "head_sha": row["head_sha"],
            "approved_at": row["approved_at"],
            "priority": row["priority"],
            "urgent": bool(row["urgent"]),
            "passes_waited": row["passes_waited"],
            "probes": row["probes"],
            "last_probe": row["last_probe"],
            "control": self._control_input(row),
            "blocked_labels": loads(row["blocked_labels"], []),
        }

    def _open_batch_summary(self, batch: dict[str, Any], now: float) -> dict[str, Any]:
        members = self._members(batch["id"])
        return {
            "id": batch["id"],
            "version": batch["version"],
            "sealed": bool(batch["sealed"]),
            "sealed_at": batch["sealed_at"],
            "base": batch["base"],
            "tip": batch["tip"],
            "size": len(members),
            "urgent": bool(batch["urgent"]),
            "emergency": batch["urgent"] == URGENT_EMERGENCY,
            "lane": batch["lane"],
            "oldest_member_age_seconds": (now - min(m["added_at"] for m in members)) if members else None,
            **self._preparation_status(batch, members, now),
            "pre_rebase": self._pre_rebase_status(batch),
            "background": self._background_status(batch, now),
        }

    def _background_status(self, batch: dict[str, Any], now: float) -> dict[str, Any]:
        """Which of the batch's background kinds is `active` (its open task, whether a worker holds it,
        and for a `prepare` task whether it prepares the `own_base` or the `pre_rebased` chain) and
        which one is `waiting` for it, with the reason; at most one kind is active per batch."""
        prepare = self.store.row(
            "tasks", {"batch_id": batch["id"], "state": TASK_OPEN, "kind": TASK_PREPARE}, order="id DESC"
        )
        pre_rebase = self._open_pre_rebase_task(batch)
        task = prepare or pre_rebase
        active = None
        waiting = None
        if task is not None:
            claimed = bool(self._active_claims(batch["id"], task_id=task["id"]))
            active = {"kind": task["kind"], "task_id": task["id"], "claimed": claimed}
            if task["kind"] == TASK_PREPARE:
                active["chain"] = "pre_rebased" if self._pre_rebased_prepare(task) else "own_base"
            if task["kind"] == TASK_PREPARE and self._pre_rebased_prepare(task):
                if claimed and self._prepare_due(batch, now):
                    waiting = {
                        "kind": TASK_PREPARE,
                        "reason": f"waits for the claimed preparation of the pre-rebased chain (task {task['id']})",
                    }
            elif task["kind"] == TASK_PREPARE:
                waiting = {
                    "kind": TASK_PRE_REBASE,
                    "reason": f"waits for prepare task {task['id']} to be adopted, released clean or superseded",
                }
            elif claimed and self._prepare_due(batch, now):
                waiting = {"kind": TASK_PREPARE, "reason": f"waits for the claimed pre_rebase task {task['id']}"}
        elif batch["pre_rebase_state"] == PRE_REBASE_CONFLICT:
            reason = self._pre_rebase_waits_for_prepare(batch)
            if reason is not None:
                waiting = {"kind": TASK_PRE_REBASE, "reason": f"waits for its preparation ({reason})"}
        return {"active": active, "waiting": waiting}

    def _preparation_status(self, batch: dict[str, Any], members: list[dict[str, Any]], now: float) -> dict[str, Any]:
        """An open batch's background preparation: the version last prepared, the open `prepare` task."""
        task = self._open_own_base_prepare(batch)
        return {
            "prepared_version": batch["prepared_version"],
            "prepared": batch["prepared_version"] is not None and batch["prepared_version"] == batch["version"],
            "prepare_task": None if task is None else self._task_status(task),
            "quiet_seconds": (now - max(m["added_at"] for m in members)) if members else None,
        }

    def _pre_rebase_status(self, batch: dict[str, Any]) -> dict[str, Any] | None:
        """An open batch's pre-rebase: `state` (`clean`, `adopted` or `conflict`), the head tip it is
        on (`onto`), its chain's `tip`, the batch tip it covers (`source`), whether it is `current` (on
        the head's tip and covering the batch's tip), the members edited in it, the open task, and
        `preparation`: `preparing` while a `prepare` task of the chain is open (`prepare_task_id`,
        `prepare_claimed`), `prepared` once the chain's current tip was built and repaired on the head's
        tip (`prepared_tip`), else `unprepared` (with `prepare_blocked`, why no task is open)."""
        task = self._open_pre_rebase_task(batch)
        if not batch["pre_rebase_state"] and task is None:
            return None
        target = self._pre_rebase_target()
        preparing = self._open_pre_rebased_prepare(batch)
        prepared = bool(batch["pre_rebase_tip"]) and batch["pre_rebase_prepared"] == batch["pre_rebase_tip"]
        if preparing is not None:
            preparation = "preparing"
        elif prepared:
            preparation = "prepared"
        else:
            preparation = "unprepared"
        return {
            "state": batch["pre_rebase_state"],
            "onto": batch["pre_rebase_onto"],
            "tip": batch["pre_rebase_tip"],
            "source": batch["pre_rebase_source"],
            "at": batch["pre_rebase_at"],
            "current": bool(batch["pre_rebase_onto"])
            and batch["pre_rebase_onto"] == target
            and batch["pre_rebase_source"] == batch["tip"],
            "edited": sorted(loads(batch["pre_rebase_edits"], {})),
            "task_id": None if task is None else task["id"],
            "preparation": preparation,
            "prepared_tip": batch["pre_rebase_prepared"],
            "prepare_task_id": None if preparing is None else preparing["id"],
            "prepare_claimed": preparing is not None
            and bool(self._active_claims(batch["id"], task_id=preparing["id"])),
            "prepare_blocked": None if preparing is not None or prepared else self._pre_rebased_prepare_blocked(batch),
        }

    def _batch_status(self, batch: dict[str, Any], now: float) -> dict[str, Any]:
        members = self._members(batch["id"])
        claims = self._active_claims(batch["id"])
        return {
            "id": batch["id"],
            "version": batch["version"],
            "sealed": bool(batch["sealed"]),
            "sealed_at": batch["sealed_at"],
            "base": batch["base"],
            "tip": batch["tip"],
            "size": len(members),
            "urgent": bool(batch["urgent"]),
            "emergency": batch["urgent"] == URGENT_EMERGENCY,
            "lane": batch["lane"],
            "oldest_member_age_seconds": (now - min(m["added_at"] for m in members)) if members else None,
            "members": [
                {
                    "number": m["number"],
                    "source_pr": m["source_pr"],
                    **self._public_pull_identity(m["number"]),
                    "accepted_head": m["accepted_head"],
                    "commit": m["commit_sha"],
                    "provenance": m["provenance"],
                    "task_id": m["task_id"],
                    "no_repair": bool(m["no_repair"]),
                    "withdrawn": bool(m["withdrawn"]),
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
            **(self._preparation_status(batch, members, now) if batch["state"] == BATCH_OPEN else {}),
        }

    def _head_status(
        self, head: dict[str, Any], now: float, settings: dict[str, Any], alerts: list[str]
    ) -> dict[str, Any]:
        candidate_ci = None if self._inert_ci_batch(head) else self._candidate_coordinator(batch_id=head["id"])
        info = self._batch_status(head, now)
        if self._inert_ci_batch(head):
            info["source_disabled"] = True
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
        wait = self._state("base_cache_wait")
        info["base_cache_wait"] = None
        if wait and wait["batch_id"] == head["id"] and wait["base"] == head["base"]:
            info["base_cache_wait"] = wait
            alert_after = int(settings["base_cache_alert_seconds"])
            if alert_after and now - wait["since"] >= alert_after:
                alerts.append(
                    f"head {head['id']} has waited {now - wait['since']:.0f}s for base {head['base']} cache to be "
                    f"published: {wait['detail']}"
                )
        if candidate_ci is not None:
            failure = self.store.get_setting(f"source-ci:freeze-error:{head['id']}")
            if failure and failure["binding"] == [head["version"], head["tip"], head["base"]] and failure["count"] >= 3:
                alerts.append(
                    f"head {head['id']}: source CI request freeze failed {failure['count']} times: {failure['detail']}"
                )
        if attempt is not None:
            ci = {
                "attempt_id": attempt["id"],
                "dispatch_id": attempt["dispatch_id"],
                "request_id": attempt["request_id"],
                "dispatch_state": attempt["dispatch_state"],
                "dispatch_error": attempt["dispatch_error"],
                "dispatched_at": attempt["dispatched_at"],
                "sent_at": attempt["sent_at"],
                "dispatch_retry_at": attempt["dispatch_retry_at"],
                "dispatch_retryable": bool(attempt["dispatch_retryable"]),
                "dispatch_failures": attempt["dispatch_failures"],
                "run_id": attempt["run_id"],
                "run_attempt": attempt["run_attempt"],
                "status": attempt["run_status"],
                "conclusion": attempt["run_conclusion"],
                "created_at": attempt["run_created_at"],
                "started_at": attempt["run_started_at"],
                "completed_at": attempt["run_completed_at"],
                "updated_at": attempt["run_updated_at"],
                "runner": attempt["runner_name"],
                "runner_labels": loads(attempt["runner_labels"], []),
                "host": attempt["host"],
                "phases": loads(attempt["phases"], []),
                "current_step": current_step(loads(attempt["phases"], [])),
                "last_progress_at": attempt["last_progress_at"],
                "last_observed_at": attempt["last_observed_at"],
                "outcome": attempt["outcome"],
                "outcome_detail": attempt["outcome_detail"],
                "verdicts": loads(attempt["verdicts"], {}),
                "html_url": attempt["html_url"],
                "infrastructure_retry": attempt["infrastructure_retry"],
                "repair_outputs_revs": loads(attempt.get("repair_outputs_revs"), []),
            }
            info["ci"] = ci
            if candidate_ci is not None:
                if attempt["dispatch_state"] == "prepared" and attempt["dispatch_failures"] >= 3:
                    alerts.append(
                        f"head {head['id']}: source CI preparation failed or send was refused {attempt['dispatch_failures']} times; "
                        f"repair configuration or retry request {attempt['request_id']}"
                    )
                ci["reconciliation_required"] = attempt["outcome_detail"].startswith("reconciliation-required:")
                if ci["reconciliation_required"]:
                    alerts.append(f"head {head['id']}: {attempt['outcome_detail']} (request {attempt['request_id']})")
            if attempt["dispatch_state"] == "rejected" and not attempt["dispatch_retryable"]:
                alerts.append(
                    f"head {head['id']} dispatch rejected; automatic retries continue; see the dispatch-failed journal entry for the reason"
                )
            if attempt["outcome"] == OUTCOME_INFRASTRUCTURE and attempt["infrastructure_retry"] >= 3:
                alerts.append(
                    f"head {head['id']} CI repeatedly failed on infrastructure: {attempt['outcome_detail']}; "
                    "automatic retries continue"
                )
            if attempt["outcome"] == OUTCOME_PENDING and attempt["dispatch_state"] in {"sending", "uncertain"}:
                alerts.append(
                    f"head {head['id']} dispatch outcome unknown (request {attempt['request_id']}); "
                    + (
                        "source CI requires verified ownership before retry_ci can proceed"
                        if candidate_ci is not None
                        else "reconciling, use retry_ci only to authorize another send"
                    )
                )
            elif (
                task is None
                and attempt["outcome"] == OUTCOME_PENDING
                and attempt["last_progress_at"]
                and attempt["dispatch_state"] != "prepared"
            ):
                last_progress = self._last_progress_at(attempt)
                silence = now - last_progress
                interval = int(settings["stall_interval_seconds"])
                if silence >= interval:
                    since = last_progress + interval
                    info["stall"] = {
                        "since": since,
                        "silence_seconds": silence,
                        "phase": (ci["phases"][-1]["name"] if ci["phases"] else None),
                    }
                    if silence >= 2 * interval:
                        if attempt["run_status"] == "completed" and (
                            ci["verdicts"].get(BUILD_CONTEXT, {}).get("state") == "success"
                        ):
                            alerts.append(
                                f"head {head['id']} verified run {attempt['run_id']} still awaits required status delivery; "
                                "automatic reporting retries continue"
                            )
                        else:
                            alerts.append(
                                f"head {head['id']} build stalled for {silence:.0f}s on {attempt['runner_name'] or 'unknown runner'} ({attempt['host'] or 'unknown host'})"
                            )
        if task is not None and not info["task"]["active_claims"]:
            unclaimed_at = task["created_at"]
            for claim in self.store.rows("claims", {"task_id": task["id"]}):
                ended_at = claim["expires_at"]
                if claim["released_at"] is not None:
                    ended_at = min(ended_at, claim["released_at"])
                if ended_at <= now:
                    unclaimed_at = max(unclaimed_at, ended_at)
            silence = now - unclaimed_at
            interval = int(settings["stall_interval_seconds"])
            if silence >= interval:
                info["stall"] = {
                    "kind": "task_unclaimed",
                    "since": unclaimed_at + interval,
                    "silence_seconds": silence,
                    "phase": task["kind"],
                }
                if silence >= 2 * interval:
                    alerts.append(f"head {head['id']} task {task['id']} ({task['kind']}) unclaimed for {silence:.0f}s")
        info["cancelled_runs"] = [
            {
                "attempt_id": a["id"],
                "version": a["batch_version"],
                "head_sha": a["head_sha"],
                "run_id": a["run_id"],
                "html_url": a["html_url"],
                "cancelled_at": a["cancelled_at"],
                "superseded_by": a["cancelled_by"],
                "reason": a["cancel_reason"],
                "result": a["cancel_result"],
            }
            for a in self.store.rows("ci_attempts", {"batch_id": head["id"]}, order="id")
            if a["cancelled_at"] is not None
        ]
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

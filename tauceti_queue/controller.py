"""The AI merge queue controller: records, claims, insertion, sealing, head reconciliation, tasks, status.

This is increment 1 of `docs/redesign/specs/ai-merge-queue.md`: one open batch receiving scripted
insertions and at most one sealed head that is rebased, built, repaired in place by agents claiming
tasks, and fast-forwarded onto the canonical branch. The controller is one object shared by the
reconciliation loop (`tauceti_queue.loop`) and the HTTP server; every public method takes the
controller lock (`ControllerLock`: the worker endpoints take it ahead of the tick, which lets
go of it around its GitHub I/O), and every record mutation is a `Store` transaction.

The controller never calls GitHub itself; it is handed a `GitHubPort`. Git work (squash, replay,
validation, provenance) is `tauceti_queue.chain` run against a local clone of the content
repository, into which the port fetches member heads and the canonical branch. GitHub side effects
(promotion, comments, closes, labels, ref retention) go through the outbox table with idempotency
keys and are drained with retries; restart reconciliation (`recover`) releases in-process claims and
leaves the outbox and worker claims to be re-driven.

Documentation of the records, state machine, task API and status schema: `docs/queue/controller.md`.
"""

from __future__ import annotations

import concurrent.futures
import contextlib
import logging
import random
import re
import secrets
import subprocess
import threading
import time
from collections.abc import Callable, Iterator, Sequence
from typing import Any

from . import admission_planning, chain, declaration_names
from .eligibility import KEEP as BLOCKING_LABELS
from .ports import (
    AGENT_INSTRUCTION,
    Adoption,
    CandidateRun,
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
# How many of a batch's previous tips a CI dispatch names in `repair_outputs_revs`.
REPAIR_OUTPUTS_PREVIOUS_TIPS = 4
# How many of the most recently landed heads lend their published tips as fetch candidates.
REPAIR_OUTPUTS_LANDED_HEADS = 2
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
    # An in-progress run counts as lost only when neither its jobs nor GitHub's direct read of the run
    # (`updated_at`) have moved for this long; until then a long build is a `stall` reason line.
    "lost_run_seconds": 14400,
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
MISSING_SOURCE_IN_FLIGHT = "missing-source-in-flight"  # `last_probe` of an input waiting for a source in the head
# The cancellation reason of a `no_repair` member the agent emptied: withdrawn until the PR it
# interacts with lands on `main` (then `needs-rebase`) or leaves the queue (then re-admitted).
PROBLEM_ANTICIPATED = "problem-anticipated"
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
# How often a worker holding a `prepare` claim renews it (`Task.extra.renew_after`): a refused renewal
# is how the worker learns that the controller took the batch back (it sealed, or the head needs the
# worker), so the interval bounds how long a superseded preparation keeps running.
PREPARE_RENEW_SECONDS = 60
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
# The subject of a squash commit bors-ng lands: `<title> (#<number>)`, with no `Source-PR` trailer.
_BORS_SUBJECT_RE = re.compile(r"\(#(?P<number>\d+)\)\s*$")
# What the agent appends to an emptied `no_repair` member's message (the repair prompt's "Do not edit").
_INTERACTING_PR_RE = re.compile(
    r"^\s*Interacting-PR:\s*(?:[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)?#(?P<number>\d+)", re.M | re.I
)
_INTERACTING_FILE_RE = re.compile(r"^\s*Interacting-File:\s*`?(?P<file>[^`\n]+?)`?\s*$", re.M | re.I)
_FINDING_RE = re.compile(r"^\s*Finding:\s*(?P<finding>\S.*?)\s*$", re.M | re.I)

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


class ControllerLock:
    """The controller lock: reentrant, with a priority lane for the worker endpoints.

    A priority acquirer (`priority()`: claim, renew, release, submit, inference reports) waits only
    for the current holder; any other acquirer also waits while a priority acquirer is waiting. So
    whenever the holder lets go, the worker endpoints go first. The tick lets go entirely around its
    GitHub I/O (`unlocked()`) and between units of work (`offer()`), so a worker request waits for at
    most one unit of the tick's work, not for the whole tick.
    """

    def __init__(self) -> None:
        self._cond = threading.Condition(threading.Lock())
        self._owner: int | None = None
        self._depth = 0
        self._priority_waiting = 0

    def _take(self, priority: bool, deadline: float | None) -> bool:
        """Under `_cond`: wait until the lock is free (and, for an ordinary acquirer, no priority
        acquirer waits), then own it once. False when `deadline` passes first."""
        if priority:
            self._priority_waiting += 1
        try:
            while self._owner is not None or (not priority and self._priority_waiting):
                if deadline is None:
                    self._cond.wait()
                    continue
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._cond.wait(remaining)
            self._owner = threading.get_ident()
            self._depth = 1
            return True
        finally:
            if priority:
                self._priority_waiting -= 1
                if not self._priority_waiting:
                    self._cond.notify_all()

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
                self._cond.notify_all()

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

    @contextlib.contextmanager
    def unlocked(self) -> Iterator[None]:
        """Let go of every level this thread holds for the body, then take them back (as an ordinary
        acquirer, so the priority requests that queued meanwhile run first). A no-op for a thread that
        does not hold the lock. The caller must not be inside a store transaction."""
        with self._cond:
            if self._owner != threading.get_ident():
                depth = 0
            else:
                depth, self._depth, self._owner = self._depth, 0, None
                self._cond.notify_all()
        try:
            yield
        finally:
            if depth:
                with self._cond:
                    self._take(False, None)
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
        self.admission = None  # primary policy intake, attached by the service in policy mode
        self.github = github
        self.clone = clone
        self.repository = repository
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
        self._lock = ControllerLock()
        # Serializes ticks: the tick lets go of `_lock` around its GitHub I/O, so the lock alone no
        # longer keeps a second tick (or drain) from interleaving with the first.
        self._ticking = threading.RLock()
        # Renewals waiting for the lock: claim token -> when the request arrived. `_expire_claims`
        # spares a claim whose renewal arrived before it expired.
        self._renewing: dict[str, float] = {}
        self._renewing_guard = threading.Lock()
        self._wake = threading.Event()
        self._reasons: list[str] = []
        # The last status document and the monotonic time it was taken, served by `status()` when the
        # lock is held by a long tick.
        self._status_snapshot: tuple[float, dict[str, Any]] | None = None
        self.canonical_branch = canonical_branch or self._discover_canonical_branch()
        self._canonical: str | None = self.store.get_setting("state:canonical_sha")
        # Labels this process has made sure exist in the repository (`_ensure_labels`).
        self._labels_ensured: set[str] = set()

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

    def _fetch_declaration_names(self, now: float) -> bool:
        """Poll/start at most two background reads, without delaying a reconciliation pass.

        Only this method mutates the store, while holding the controller lock; background
        threads only use the App adapter. Recheck accepted revision and active input state on
        completion, so late evidence cannot revive a cancelled input or follow a changed head.
        Unknown/missing producer evidence is final for this revision; transport exceptions have
        three attempts with backoff. Persisted attempts bound retries across service restarts.
        """
        if not hasattr(self.github, "declaration_names"):
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
                self.github.declaration_names, self.repository, head
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
            now = self.clock()
            existing = self._input_for(pr)
            if existing is not None:
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
            no_repair = self._carries_no_repair(getattr(pull, "labels", None))
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

    def _no_repair_labels(self) -> set[str]:
        return {str(label).lower() for label in self.setting("no_repair_labels") or ()}

    def _carries_no_repair(self, labels: Any) -> bool:
        """Whether a PR's labels include one of `no_repair_labels` (case-insensitive)."""
        return bool({str(label).lower() for label in labels or ()} & self._no_repair_labels())

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

    def unapprove(self, repo: str, pr: int, actor: str = "", *, head_sha: str = "") -> dict[str, Any]:
        """`bors r-`: a waiting input is withdrawn; a batch member is not (membership is fixed)."""
        return self._withdraw(repo, pr, "unapproved", actor, head_sha=head_sha)

    def cancel(self, repo: str, pr: int, actor: str = "", *, head_sha: str = "") -> dict[str, Any]:
        """`bors cancel`: the same as `unapprove`; the head is never cancelled."""
        return self._withdraw(repo, pr, "cancelled", actor, head_sha=head_sha)

    def _withdraw(self, repo: str, pr: int, reason: str, actor: str, *, head_sha: str = "") -> dict[str, Any]:
        with self._lock:
            self._check_repo(repo)
            now = self.clock()
            current = self._input_for(pr)
            if (
                self.admission is not None
                and reason in ("unapproved", "cancelled")
                and (current is None or current["state"] != INPUT_INSERTED)
            ):
                with self.store.transaction():
                    self.admission.withdraw(pr, head_sha or (current or {}).get("head_sha", ""))
                    if current is not None and current["state"] in (INPUT_WAITING, INPUT_RESERVED):
                        self.store.update("inputs", {"id": current["id"]}, state=INPUT_CANCELLED, reason=reason)
                        self.store.journal(now, "withdrawn", number=pr, reason=reason, actor=actor)
                        self._refresh_snapshot()
                        return {"withdrawn": True, "reason": reason}
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
        if action in ("labeled", "unlabeled") and self._no_repair_event(current, payload, now):
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
        try:
            return bool(self.admins.get(login, login))
        except GitHubUnavailable as error:
            self.log.warning("admin check for %s failed with nothing cached: %s", login, error)
            return None

    def claim_task(self, worker_identity: str, github_login: str) -> Task | None:
        """Hand an open task to an authorized worker under a renewable claim, within `repair_concurrency`.

        `repair_concurrency` bounds the live task claims in total. Within it, tasks are offered in
        two tiers, each by kind priority then age (`TASK_PRIORITY`): first a head task (`repair` or
        `rebase_conflict`); then an open batch's background task (`pre_rebase`, then `prepare`).
        Each batch has at most one live worker claim, including when its task changes. Background
        claims number at most `repair_concurrency - 1`, so head work never waits behind background
        work. With two workers one repairs the head while the other prepares or pre-rebases the
        next batch; a worker waits when no distinct batch needs work.

        The admin check (a cached GitHub read) runs before the lock is taken, and the claim takes the
        lock's priority lane, so a claim answers while a tick is running.
        """
        admin = self._repository_admin(github_login)
        with self._lock.priority():
            now = self.clock()
            self._expire_claims(now)
            if not admin:
                reason = "not-admin" if admin is False else "github-unavailable"
                self.store.journal(now, "claim-refused", worker=worker_identity, login=github_login, reason=reason)
                return None
            concurrency = int(self.setting("repair_concurrency"))
            claimable = self._claimable_tasks()
            if len(self._active_task_claims()) >= concurrency:
                if claimable and claimable[0][0]["kind"] not in BACKGROUND_TASKS:
                    self._count("claim_collisions")
                    self.store.journal(
                        now,
                        "claim-refused",
                        worker=worker_identity,
                        reason="concurrency",
                        task_id=claimable[0][0]["id"],
                    )
                return None
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
        """The next task on a batch without a live worker claim: a head task, else background while
        background claims number fewer than `concurrency - 1` (one slot is always left for head work,
        so with `repair_concurrency` 1 nothing runs in the background). Selection and insertion of
        the claim share the controller lock; concurrent requests cannot select the same batch.
        """
        active = self._active_task_claims()
        held_batches = {claim["batch_id"] for claim in active}
        unheld = [pair for pair in claimable if pair[1]["id"] not in held_batches]
        for pair in unheld:
            if pair[0]["kind"] not in BACKGROUND_TASKS:
                return pair
        background = [claim for claim in active if claim["operation"] in BACKGROUND_OPERATIONS]
        if len(background) < concurrency - 1:
            for task, batch in unheld:
                if task["kind"] in BACKGROUND_TASKS:
                    return task, batch
        return None

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
            if batch["version"] != task["batch_version"]:
                continue
            wanted = BATCH_OPEN if task["kind"] == TASK_PREPARE else BATCH_HEAD
            if batch["state"] != wanted:
                continue
            found.append((task, batch))
        found.sort(key=lambda pair: (TASK_PRIORITY.get(pair[0]["kind"], 0), pair[0]["created_at"], pair[0]["id"]))
        return found

    def _active_task_claims(self) -> list[dict[str, Any]]:
        """Every worker's live task claim, on any batch: what `repair_concurrency` bounds."""
        return [
            row
            for row in self._live_claims(self.store.rows("claims", {"released_at": None}))
            if row["task_id"] is not None
        ]

    def renew_task(self, task_id: int, claim_token: str) -> float:
        """Extend the claim by `claim_ttl_seconds` from now.

        The claim's expiry is judged at the request's arrival, not when it gets the lock: a renewal that
        arrived before the claim expired extends it even if a busy tick kept it waiting past the expiry
        (and `_expire_claims` spares the claim while such a renewal waits).
        """
        arrived = self.clock()
        with self._renewing_guard:
            self._renewing[claim_token] = min(arrived, self._renewing.get(claim_token, arrived))
        try:
            with self._lock.priority():
                claim = self._claim_for(task_id, claim_token, arrived)
                now = self.clock()
                expires = now + int(self.setting("claim_ttl_seconds"))
                self.store.update("claims", {"id": claim["id"]}, expires_at=expires, renewed_at=now)
                return expires
        finally:
            with self._renewing_guard:
                if self._renewing.get(claim_token) == arrived:
                    del self._renewing[claim_token]

    def release_task(self, task_id: int, claim_token: str, reason: str, error: str = "") -> None:
        """A worker gives a task back. Reason `inference_unavailable` counts toward the inference hold.

        A `prepare` task released as `prepared_clean` (its batch built without an error, so there was
        nothing to repair) records the batch version as prepared; one released after the agent or the
        push failed is resolved as `failed` and not offered again at that version. Any other release
        leaves the task open for the next claim, as for the head's tasks.
        """
        with self._lock.priority():
            now = self.clock()
            claim = self._claim_for(task_id, claim_token, now)
            task = self.store.row("tasks", {"id": task_id})
            with self.store.transaction():
                self.store.update("claims", {"id": claim["id"]}, released_at=now, release_reason=reason)
                self.store.journal(now, "released", claim["batch_id"], task_id=task_id, reason=reason, error=error)
                if task is not None and task["kind"] == TASK_PREPARE and task["state"] == TASK_OPEN:
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

    def submit_task(self, task_id: int, claim_token: str, tip_sha: str, worker_notes: str = "") -> Adoption:
        """Validate a proposed chain against the task and adopt it as the batch's next version.

        A late result (expired claim, superseded task or batch version) is rejected but its commits
        stay retained under a submissions ref. See `Adoption` for the reason codes.
        """
        with self._lock.priority():
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
            if task["kind"] == TASK_PRE_REBASE:
                if not self._pre_rebase_task_current(task, batch):
                    return reject("version-mismatch")
            elif self._pre_rebased_prepare(task):
                if not self._pre_rebased_prepare_current(task, batch):
                    return reject("version-mismatch")
            elif (
                task["state"] != TASK_OPEN
                or batch["version"] != task["batch_version"]
                or batch["state"] != (BATCH_OPEN if task["kind"] == TASK_PREPARE else BATCH_HEAD)
            ):
                return reject("version-mismatch")
            if retained is None:
                return reject("tip-unavailable")
            members = self._task_members(task)
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
            edited_no_repair = self._no_repair_edits(task, members, comparison)
            if edited_no_repair:
                violations = [
                    chain.Violation(
                        "no-repair-edited",
                        f"{m.source_pr} opted out of repair: leave its content alone, or reduce it to an empty commit "
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
                self.store.update("members", {"id": member["id"]}, **values)
                provenance[member["source_pr"]] = values.get("provenance", member["provenance"])
            # An adopted preparation keeps its batch open: the repaired members are what it seals with.
            prepared = {"prepared_version": version} if task["kind"] == TASK_PREPARE else {}
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
                self._discard_pre_rebase("head-shrunk", now)
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
        with self.store.transaction():
            self._set_pre_rebase(
                batch, PRE_REBASE_ADOPTED, now, tip=tip_sha, onto=task["onto"], source=task["tip"], edits=edits
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
            )
        self.report_inference(claim["worker"], True)
        self._wake.set()
        return Adoption(True, "adopted", task["id"], batch["id"], batch["version"], tip_sha, ref, provenance=provenance)

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

    def _task_members(self, task: dict[str, Any]) -> list[dict[str, Any]]:
        """The members a task's chain covers: the batch's, or for a `pre_rebase` task and a `prepare`
        task of the pre-rebased chain those in its `base..tip` (members inserted since are replayed
        onto its result afterwards)."""
        members = self._members(task["batch_id"])
        if task["kind"] != TASK_PRE_REBASE and not self._pre_rebased_prepare(task):
            return members
        covered = {commit.source_pr for commit in chain.chain_commits(self.clone, task["base"], task["tip"])}
        return [member for member in members if member["source_pr"] in covered]

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
                )
            )
        refs = [self.batch_ref(batch["id"], task["batch_version"])] + [m.retained_ref for m in members]
        extra: dict[str, Any] = {"canonical_branch": self.canonical_branch, "claim_id": claim_id}
        if task["kind"] == TASK_PREPARE:
            extra["renew_after"] = PREPARE_RENEW_SECONDS
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
        return Task(
            task_id=task["id"],
            kind=task["kind"],
            repository=self.repository,
            batch_id=batch["id"],
            batch_version=task["batch_version"],
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
            extra=extra,
            warm_revs=self._warm_revs(task, batch),
        )

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

    # --- the reconciliation pass -------------------------------------------------------------------

    def tick(self) -> list[str]:
        """One reconciliation pass; returns the reasons the queue is waiting (also reported in status).

        The pass ends by taking the status snapshot `status()` serves while a later pass holds the lock.
        """
        with self._ticking, self._lock:
            try:
                return self._tick()
            finally:
                self._refresh_snapshot()

    def _tick(self) -> list[str]:
        """The pass, holding the controller lock except around its GitHub I/O (the outbox drain, the
        canonical fetch, each probe's fetch and replay) and offering it to the waiting worker
        requests between steps (`ControllerLock`), so a worker endpoint waits for one step at most."""
        offer = self._lock.offer
        self._reasons = []
        now = self.clock()
        self._expire_claims(now)
        self._drain_outbox(now)
        offer()
        try:
            with self._lock.unlocked():
                canonical = self._fetch_canonical()
        except Exception as error:  # noqa: BLE001 - the pass reports the failure and waits for the next one
            self._reasons.append(f"canonical branch unavailable: {error}")
            return list(self._reasons)
        self._observe_canonical(canonical, now)
        self._follow_anticipated(now)
        hold = self._state("inference_hold")
        if hold:
            self._reasons.append(f"inference hold since {hold['since']:.0f}: {hold['last_error']}")
        offer()
        self._fetch_declaration_names(now)
        self._insertion_passes(now)
        for step in (
            self._supersede_stale_pre_rebase,
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
            withdrawn = {
                m["commit_sha"] for m in self.store.rows("members", {"withdrawn": 1, "removed": 0}) if m["commit_sha"]
            }
            try:
                for commit in chain.chain_commits(self.clone, previous, canonical):
                    source = commit.source_pr
                    if commit.sha in withdrawn:
                        continue  # a withdrawn member's empty commit lands nothing
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
                edited_numbers = {int(pr.rsplit("#", 1)[1]) for pr in edited}
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
                if any(c["operation"] == f"task:{TASK_PREPARE}" for c in self._active_claims(batch["id"])):
                    # A `prepare` task holds this batch: insertion goes on to the next open batch
                    # (created on the canonical commit when the limit allows) instead of waiting.
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
            self.repository,
            row["number"],
            row["head_sha"],
            title=row["title"],
            description=row["description"],
            author=row["author"],
            coauthors=None if loads(row["coauthors"]) is None else tuple(loads(row["coauthors"])),
        )
        chain_id = f"probe-{batch['id']}-{secrets.token_hex(4)}"
        failure: chain.ChainError | None = None
        result: chain.ChainResult | None = None
        gaps: list[tuple[str, str]] = []
        scan_unavailable: str | None = None
        try:
            with self._lock.unlocked():
                if not self._fetch_head(row["number"], row["head_sha"]):
                    raise chain.ChainError(f"accepted head {row['head_sha']} is not available")
                result = chain.build(self.clone, batch["tip"], [member], chain_id=chain_id)
                gaps, scan_unavailable = (
                    self._new_import_gaps(batch["tip"], result) if result.status == chain.CLEAN else ([], None)
                )
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
        current = self.store.row("batches", {"id": batch["id"]})
        prepared = any(c["operation"] == f"task:{TASK_PREPARE}" for c in self._active_claims(batch["id"]))
        if current is None or current["version"] != batch["version"] or current["state"] != BATCH_OPEN or prepared:
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
                # Every missing source is in the head or another open batch: the PR is early, not
                # wrong. It waits, and is not probed against any batch on this base again (a fresh
                # open batch on the same canonical commit lacks the source just the same, so unlike
                # a conflict this is no reason to open one); it is probed once a base moves, by which
                # time the provider has landed or left the queue.
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
            self.store.update(
                "batches", {"id": batch["id"]}, version=version, tip=result.tip, urgent=batch["urgent"] or row["urgent"]
            )
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

    def _planner(self) -> admission_planning.Planner:
        if not hasattr(self, "_admission_planner"):
            self._admission_planner = admission_planning.Planner(self.clone)
        return self._admission_planner

    def _new_import_gaps(self, tip: str, result: chain.ChainResult) -> tuple[list[tuple[str, str]], str | None]:
        """Project imports with no source in the prospective tree that the current tip does not have.

        Comparing the two trees charges every new gap to the PR being inserted: a gap already on the
        batch's base appears in both scans, and a batch-mate that removed a source was refused at its
        own insertion. Returns the sorted new gaps, or `([], reason)` when either scan is unavailable,
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
        """The batches (head or other open ones) whose tips provide every missing source, else None."""
        candidates = [b for b in [self._head(), *self._open_batches()] if b is not None and b["id"] != batch["id"]]
        found: list[int] = []
        for _, module in gaps:
            provider = module.replace(".", "/") + ".lean"
            holder = next((b for b in candidates if b["tip"] and self._tree_has(b["tip"], provider)), None)
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
        claims = self._active_claims(batch["id"])
        if claims:
            # Keep the batch open while its owner prepares or pre-rebases it. Adoption uses the
            # ordinary open-batch validation, and the next head keeps the completed work.
            self._reasons.append(f"open batch {batch['id']} is claimed; sealing waits for the release")
            return
        with self.store.transaction():
            # Unclaimed background offers give way to the new head's own work.
            self._supersede_prepare(batch["id"], "sealed", now)
            for stale in self.store.rows(
                "tasks", {"batch_id": batch["id"], "state": TASK_OPEN, "kind": TASK_PRE_REBASE}
            ):
                self.store.update(
                    "tasks", {"id": stale["id"]}, state=TASK_SUPERSEDED, resolved_at=now, resolution="sealed"
                )
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
            # A speculative repair runs alongside its own candidate CI, whose observation
            # continues without permitting promotion or replacing the claimed task.
            active = self._open_task(head)
            if active is not None and self._prediction_task(active):
                attempt = self._current_attempt(head)
                if attempt is not None and attempt["outcome"] == OUTCOME_PENDING:
                    self._observe_attempt(head, attempt, now)
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
                return
            head = self._head()
            assert head is not None
        task = self._open_task(head)
        if task is not None:
            if self._prediction_task(task):
                attempt = self._current_attempt(head)
                if attempt is not None and attempt["outcome"] == OUTCOME_PENDING:
                    self._observe_attempt(head, attempt, now)
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
        if not self.setting("prepare_open_batches") or self._state("inference_hold"):
            return
        for batch in self._open_batches():
            if not self._prepare_due(batch, now):
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
            if pre_rebase is not None:
                with self.store.transaction():
                    self._supersede_pre_rebase_tasks(batch["id"], "prepare", now)
                    self.store.journal(
                        now, "pre-rebase-superseded", batch["id"], task_id=pre_rebase["id"], resolution="prepare"
                    )
            self._create_task(batch, TASK_PREPARE, batch["base"], [], now)
            self._note_reason(f"open batch {batch['id']} waits for a worker to prepare it")

    def _pre_rebase_waits_for_prepare(self, batch: dict[str, Any]) -> str | None:
        """Why a `pre_rebase` task may not be created for the batch yet; None once it may.

        A batch that background preparation applies to (`prepare_open_batches` on, at least
        `prepare_min_members` members) is pre-rebased by an agent only from its prepared chain: a
        preparation adopted later would rewrite the chain under the pre-rebase and discard the
        agent's resolutions. So the task waits while a `prepare` task is open and until the batch is
        prepared at its current version, or its preparation at that version failed.
        """
        prepare = self._open_own_base_prepare(batch)
        if prepare is not None:
            held = "claimed" if self._active_claims(batch["id"], task_id=prepare["id"]) else "open"
            return f"prepare task {prepare['id']} is {held}"
        if not self.setting("prepare_open_batches"):
            return None
        if len(self._members(batch["id"])) < max(1, int(self.setting("prepare_min_members"))):
            return None
        if batch["prepared_version"] == batch["version"]:
            return None
        settled = self.store.row(
            "tasks",
            {"batch_id": batch["id"], "batch_version": batch["version"], "kind": TASK_PREPARE, "resolution": "failed"},
        )
        if settled is not None:
            return None
        return f"not yet prepared at v{batch['version']}"

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
        if not predicted or attempted:
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
        if attempt is None or attempt["head_sha"] != head["tip"]:
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
            and task["onto"] == self._pre_rebase_target()
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

    def _discard_pre_rebase(self, reason: str, now: float) -> None:
        """The head was reshaped (rebuilt, or a member shrunk to nothing): forget every open batch's
        pre-rebase, edits included, and supersede its task; the next pass replays from scratch."""
        for batch in self._open_batches():
            if not batch["pre_rebase_state"] and self._open_pre_rebase_task(batch) is None:
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
            with self.store.transaction():
                self._supersede_pre_rebase_tasks(batch["id"], "head-moved", now)
                self.store.journal(now, "pre-rebase-superseded", batch["id"], task_id=task["id"], onto=task["onto"])
        self._supersede_stale_pre_rebased_prepare(now)

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
        head = self._head()
        if self._pre_rebase_blocked(head) is not None:
            return
        assert head is not None
        onto = head["tip"]
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
            and task["onto"] == self._pre_rebase_target()
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
        if len(self._members(batch["id"])) < max(1, int(self.setting("prepare_min_members"))):
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
                m["number"]: loads((self.store.row("inputs", {"id": m["input_id"]}) or {}).get("declaration_names"), {})
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
        `pre_rebase_onto`); members inserted after the pre-rebase are replayed onto it first. The
        members take their commits from the pre-rebased chain, and those an agent edited in a
        `pre_rebase` task are recorded as edited with its explanation. False (nothing changed) when
        there is no such pre-rebase, an extension conflicts or the chain's members do not match.
        """
        if head["pre_rebase_state"] not in PRE_REBASE_USABLE or head["pre_rebase_onto"] != canonical:
            return False
        tip = head["pre_rebase_tip"]
        source = head["pre_rebase_source"]
        if not tip or not source or not chain.is_ancestor(self.clone, source, head["tip"]):
            return False
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
                self._record_tip(head["id"], version, result.tip, now)
                superseded = 0
                for stale in self.store.rows("tasks", {"batch_id": head["id"], "state": TASK_OPEN}):
                    self.store.update(
                        "tasks", {"id": stale["id"]}, state=TASK_SUPERSEDED, resolved_at=now, resolution="rebuilt"
                    )
                    superseded += 1
                self._retain(self.batch_ref(head["id"], version), result.tip)
                self._count("head_rebuilds")
                self._discard_pre_rebase("head-rebuilt", now)
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

    def retry_ci(self, repo: str, actor: str) -> dict[str, Any]:
        """Operator: forget the head's exhausted infrastructure failures so the next tick re-dispatches.

        Refused (`reason: live-run`) while GitHub reports a run for the head's tip queued or in
        progress, since the re-dispatch would cancel it; `github-unavailable` when that cannot be checked.
        """
        with self._lock:
            self._check_repo(repo)
            head = self._head()
            if head is None:
                return {"retried": False, "reason": "no-head"}
            now = self.clock()
            attempts = self._tip_attempts(head)
            if attempts:
                # A new dispatch would cancel a live run for the tip (pr-build.yml's concurrency group).
                try:
                    run = self._live_run(head, attempts)
                except Exception as error:  # noqa: BLE001
                    return {"retried": False, "reason": "github-unavailable", "detail": str(error)}
                if run is not None:
                    self.store.journal(now, "retry-ci-refused", head["id"], actor=actor, run_id=run.run_id)
                    return {
                        "retried": False,
                        "reason": "live-run",
                        "detail": f"run {run.run_id} for the tip is {run.status}; a re-dispatch would cancel it",
                        "run_id": run.run_id,
                        "status": run.status,
                    }
            count = self.store.update(
                "ci_attempts",
                {"batch_id": head["id"], "batch_version": head["version"], "outcome": OUTCOME_INFRASTRUCTURE},
                outcome=OUTCOME_SUPERSEDED,
            )
            self.store.journal(now, "retry-ci", head["id"], actor=actor, forgotten=count)
            self._wake.set()
            return {"retried": True, "forgotten": count}

    def _tip_attempts(self, head: dict[str, Any]) -> list[dict[str, Any]]:
        """The attempts at the head's current version for its current tip and base, oldest first."""
        return [
            a
            for a in self.store.rows("ci_attempts", {"batch_id": head["id"], "batch_version": head["version"]})
            if a["head_sha"] == head["tip"] and a["base_sha"] == head["base"]
        ]

    def _live_run(self, head: dict[str, Any], attempts: list[dict[str, Any]]) -> CandidateRun | None:
        """The newest run GitHub reports for the head's tip since its first attempt, when it is still
        queued or in progress; `None` when there is none. Raises when GitHub cannot be asked."""
        run = self.github.find_candidate_run(
            self.repository,
            batch_id=str(head["id"]),
            head_sha=head["tip"],
            base_sha=head["base"],
            since=min(a["dispatched_at"] for a in attempts),
        )
        if run is None or run.status not in LIVE_RUN_STATUSES:
            return None
        # A run the controller cancelled may still read as live for a moment; it is not re-adopted.
        return None if run.run_id in self._cancelled_run_ids(head["id"]) else run

    def _readopt_live_run(self, head: dict[str, Any], now: float) -> bool:
        """Before any re-dispatch of a tip that was dispatched before: re-adopt a run for it that GitHub
        still reports queued or in progress, instead of dispatching a duplicate beside it.

        A duplicate is not only wasted: pr-build.yml's concurrency group cancels the live staging run
        when the second one starts. So a live run is re-adopted whatever its attempt was classified as
        (lost, no-run, superseded). The newest attempt for the tip becomes pending again, bound to the
        live run, so the staging candidate branch named after the attempt stays the one CI verifies.
        Returns whether the dispatch must not happen this tick (re-adopted, or GitHub could not be asked).
        """
        attempts = self._tip_attempts(head)
        if not attempts:
            return False
        try:
            run = self._live_run(head, attempts)
        except Exception as error:  # noqa: BLE001
            self._reasons.append(f"head {head['id']}: cannot check for a live CI run before re-dispatch: {error}")
            return True
        if run is None:
            return False
        attempt = attempts[-1]
        with self.store.transaction():
            self.store.update(
                "ci_attempts",
                {"id": attempt["id"]},
                outcome=OUTCOME_PENDING,
                outcome_detail="",
                run_id=run.run_id,
                run_status=run.status,
                last_progress_at=now,
                needs_observation=1,
            )
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
        reader = getattr(self.github, "read_run", None)
        if reader is None:
            raise ControllerError("no-read-run", "the GitHub port cannot read a run directly")
        return reader(self.repository, run_id, with_jobs=with_jobs)

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
        cancel request is logged and retried once through the outbox (`cancel_run`); it never holds
        anything back. The attempt is recorded as cancelled either way, so observation and the
        re-adoption of live runs leave its run alone.
        """
        cancel = getattr(self.github, "cancel_run", None)
        if cancel is None:
            return
        for attempt in self.store.rows("ci_attempts", {"batch_id": batch_id}, order="id"):
            if attempt["id"] == by or attempt["cancelled_at"] is not None:
                continue
            if not attempt["run_id"] or attempt["run_status"] == "completed":
                continue
            run_id = str(attempt["run_id"])
            try:
                result = cancel(self.repository, run_id)
                error = None
            except Exception as caught:  # noqa: BLE001 - a failed cancel is retried once, never blocks
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
                    self._enqueue("cancel_run", f"cancel-run:{run_id}", {"run_id": run_id, "attempt_id": attempt["id"]})
            if error is not None:
                self.log.warning(
                    "head %s: cancelling superseded CI run %s failed, retrying once via the outbox: %s",
                    batch_id,
                    run_id,
                    error,
                )

    def _dispatch(self, head: dict[str, Any], now: float, retry: int) -> None:
        if not self.setting("publication_enabled"):
            self._reasons.append(
                f"publication disabled: would dispatch CI for head {head['id']} v{head['version']} ({head['tip'][:12]} on {head['base'][:12]})"
            )
            return
        if self._readopt_live_run(head, now):
            return
        members = [m["number"] for m in self._members(head["id"])]
        # A head from before `batch_tips` existed has its current tip recorded here, so that the
        # dispatch after its next rebase names it.
        self._record_tip(head["id"], head["version"], head["tip"], now)
        repair_outputs_revs = self._repair_outputs_revs(head)
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
            repair_outputs_revs=dumps(repair_outputs_revs),
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
                repair_outputs_revs=repair_outputs_revs,
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
                repair_outputs_revs=repair_outputs_revs,
            )
        self._cancel_superseded_runs(head["id"], now, reason=f"superseded by attempt {attempt_id}", by=attempt_id)

    def _check_lost_run(
        self, head: dict[str, Any], attempt: dict[str, Any], run: CandidateRun, values: dict[str, Any], now: float
    ) -> None:
        """Decide whether an unfinished run is lost; updates `values` with the verdict when it is.

        A queued (or otherwise waiting) run is never lost. An in-progress run is lost only when its
        jobs have reported nothing for `lost_run_seconds` and a direct read of the run still says
        `in_progress` with an `updated_at` at least that old; short of that a long silence is a
        `stall` reason line, never an infrastructure verdict.
        """
        threshold = int(self.setting("lost_run_seconds"))
        silence = now - values["last_progress_at"]
        if silence < threshold:
            return
        stall = f"stall: head {head['id']} run {run.run_id} is {run.status or 'unknown'} with no job update for {silence:.0f}s"
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
                return
            if run is None:
                values.update(
                    outcome=OUTCOME_INFRASTRUCTURE,
                    outcome_detail=f"lost-run: run {attempt['run_id']} is no longer on GitHub",
                )
                self.store.journal(
                    now, "ci-infrastructure", head["id"], attempt_id=attempt["id"], detail=values["outcome_detail"]
                )
                self.store.update("ci_attempts", {"id": attempt["id"]}, **values)
                return
        if run is None:
            if now - attempt["dispatched_at"] >= int(self.setting("dispatch_timeout_seconds")):
                values.update(outcome=OUTCOME_INFRASTRUCTURE, outcome_detail="no-run: the dispatch produced no run")
                self.store.journal(
                    now, "ci-infrastructure", head["id"], attempt_id=attempt["id"], detail=values["outcome_detail"]
                )
            self.store.update("ci_attempts", {"id": attempt["id"]}, **values)
            return
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
        values.update(
            run_id=run.run_id,
            run_attempt=run.attempt,
            run_status=run.status,
            run_conclusion=run.conclusion,
            run_created_at=run.created_at,
            run_started_at=run.started_at,
            run_completed_at=run.completed_at,
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
        # A withdrawn member's empty commit lands with the head but the PR does not: it is not marked
        # landed, commented on or closed (`_anticipate` told its author why).
        everyone = self._members(batch["id"])
        withdrawn = [m["number"] for m in everyone if m["withdrawn"]]
        members = [m for m in everyone if not m["withdrawn"]]
        with self.store.transaction():
            self.store.update("batches", {"id": batch["id"]}, state=BATCH_LANDED, landed_at=now, landed_how=how)
            self._clear_admission_state(batch["id"])
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
                now,
                "landed",
                batch["id"],
                how=how,
                tip=batch["tip"],
                members=[m["number"] for m in members],
                **({"withdrawn": withdrawn} if withdrawn else {}),
            )
            if self.setting("publication_enabled"):
                for member in members:
                    self._enqueue_landing_effects(batch, member)
            self._interacting_landed({m["number"] for m in members}, now)
        self._cancel_superseded_runs(batch["id"], now, reason="batch landed")
        self._wake.set()

    def _enqueue_landing_effects(self, batch: dict[str, Any], member: dict[str, Any]) -> None:
        number = member["number"]
        body = self.member_comment(batch, member)
        if member["provenance"] != "clean":
            diff = self._landed_diff(member)
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
            if label and not self._landed_outside_queue(member):
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
        marker = f"<!-- {COMMENT_MARKER} landed:{self.repository}#{number}:{batch['id']}:{member['commit_sha']} -->"
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
        if member["provenance"] != "clean":
            commit_url = f"https://github.com/{self.repository}/commit/{member['commit_sha']}"
            lines += [
                "",
                f"The merge queue edited this contribution while integrating batch {batch['id']}; it landed as [`{member['commit_sha'][:12]}`]({commit_url}).",
                "",
            ]
            lines += [f"> {line}" if line else ">" for line in member["explanation"].splitlines()]
            lines += self._diff_lines(member, commit_url)
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

    def _diff_lines(self, member: dict[str, Any], commit_url: str) -> list[str]:
        """The landing comment's diff of the landed commit against the accepted head, in a collapsed block."""
        diff = self._landed_diff(member)
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

    def _mentioned_prs(self, text: str, exclude: int) -> list[int]:
        found: list[int] = []
        for match in _PR_MENTION_RE.finditer(text):
            repo = match["repo"]
            number = int(match["number"])
            if (repo is None or repo == self.repository) and number != exclude and number not in found:
                found.append(number)
        return found

    # --- the no-repair opt-out: problem-anticipated withdrawals ----------------------------------

    def _attribution(self, explanation: str, exclude: int) -> dict[str, Any]:
        """The interacting PR, file and finding the agent appended to an emptied `no_repair` member's
        message (`Interacting-PR:`, `Interacting-File:`, `Finding:` lines); the PR falls back to the
        first other PR the text mentions. Missing parts are None."""
        number = None
        match = _INTERACTING_PR_RE.search(explanation)
        if match and int(match["number"]) != exclude:
            number = int(match["number"])
        if number is None:
            mentioned = self._mentioned_prs(explanation, exclude=exclude)
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
        repository = self.repository.replace(".", "\\.")
        pattern = rf"(\(#{number}\)$|^{chain.SOURCE_PR_TRAILER}: {repository}#{number}$)"
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
        """
        attribution = self._attribution(explanation, member["number"])
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
        lines = [
            f"This PR carries {labels}, so the merge queue does not edit it. Integrating it in batch {batch['id']} "
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
        return [row for row in rows if loads(row["anticipated"], {}).get("stage") == ANTICIPATED_PENDING]

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
                no_repair=int(self._carries_no_repair(getattr(pull, "labels", None))),
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
        performed = 0
        retains = [e for e in due if e["kind"] == "retain_ref"]
        if len(retains) > 1 and hasattr(self.github, "update_refs"):
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
            if current["kind"] == "promote":
                try:
                    result = self._perform(current["kind"], loads(current["payload"], {}), now)
                except Exception as error:  # noqa: BLE001 - every adapter failure is recorded and retried
                    self._outbox_failed(current, loads(current["history"], []), error, now)
                else:
                    self._outbox_done(current, loads(current["history"], []), result, now)
                return
        error: Exception | None = None
        result = ""
        try:
            with self._lock.unlocked():
                result = self._perform(current["kind"], loads(current["payload"], {}), now)
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
                self.github.update_refs(self.repository, updates)
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
            next_attempt_at=getattr(error, "retry_at", None) or now + backoff,
        )
        self.log.warning("outbox %s %s failed (attempt %s): %s", entry["kind"], entry["key"], attempts, error)

    def _ensure_labels(self, repo: str, labels: list[dict[str, str]]) -> None:
        """Create each label the repository may lack, once per process: adding a missing label to a PR
        would otherwise create it with GitHub's default colour and no description."""
        for label in labels:
            if label["name"] in self._labels_ensured:
                continue
            self.github.ensure_label(repo, label["name"], label["color"], label["description"])
            self._labels_ensured.add(label["name"])

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
            if "admission_generation" in payload:
                work = self.store.row("admission_work", {"repository": repo, "number": payload["number"]})
                if work is None or work["generation"] != payload["admission_generation"]:
                    return "stale-admission"
                pull = self.github.fetch_pr(repo, payload["number"])
                if pull.head_sha != payload["head_sha"] or pull.state != "open" or pull.draft:
                    return "stale-head"
                work = self.store.row("admission_work", {"repository": repo, "number": payload["number"]})
                if work["generation"] != payload["admission_generation"]:
                    return "stale-admission"
            self._ensure_labels(repo, payload.get("ensure", []))
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
        if kind == "cancel_run":
            # The one retry of a cancel that failed inline: a second failure is recorded, not retried.
            try:
                return self.github.cancel_run(repo, payload["run_id"])
            except Exception as error:  # noqa: BLE001
                self.log.warning("retry of cancelling CI run %s failed, giving up: %s", payload["run_id"], error)
                return f"gave-up: {str(error)[:200]}"
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
                "open_batch": None
                if open_batch is None
                else self._batch_status(open_batch, now) | {"pre_rebase": self._pre_rebase_status(open_batch)},
                "open_batches": [self._open_batch_summary(batch, now) for batch in open_batches],
                "head": head_status,
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
                "updated_at": attempt["run_updated_at"],
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
                "repair_outputs_revs": loads(attempt.get("repair_outputs_revs"), []),
            }
            info["ci"] = ci
            if task is None and attempt["outcome"] == OUTCOME_PENDING and attempt["last_progress_at"]:
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

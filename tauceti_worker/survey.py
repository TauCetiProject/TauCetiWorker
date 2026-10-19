"""tauceti_worker.survey — classify every open PR per work-kind into the read-only Survey that the
picker, `status`, and the TUI all consume."""

from __future__ import annotations

import hashlib
import json
import math
import os
import random
import re
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from .config import Config, roadmap_only, roadmap_skip
from .config import max_open_prs as configured_max_open_prs
from .constants import (
    AUTO_STAGES,
    BUMP_HEAD_PREFIX,
    CONTEST_CLAIM_TTL,
    EX_NOPROGRESS,
    LINT_REPAIR_HEAD_PREFIX,
    MAX_BUMP_ATTEMPTS,
    MAX_BUMP_PR_ATTEMPTS,
    MAX_CI_ATTEMPTS,
    MAX_CI_PR_ATTEMPTS,
    MAX_FIX_ATTEMPTS,
    MAX_LINT_REPAIR_ATTEMPTS,
    MAX_LINT_REPAIR_PR_ATTEMPTS,
    MAX_PROGRESS_ERRORS,
    MAX_REBASE_ATTEMPTS,
    MAX_REVIEW_CONTESTS,
    MAX_REVIEW_CONTESTS_PER_RUBRIC,
    MAX_REVIEW_ERRORS,
    PROGRESS,
    PROGRESS_ATTEMPT_GAP,
    PROGRESS_ERROR_KEY,
    PROGRESS_REF,
    PROGRESS_TTL,
    REVIEW_AFFINITY_GRACE_S,
    REVIEW_AGE_CAP_S,
    REVIEW_AGE_SCALE_S,
    STATUS_LABELS,
    TAUCETI,
    TAUCETI_OWNER,
)
from .github import GitHub, GitHubError, _parse_iso8601, can_push, me
from .review_state import Meta, ReviewState
from .survey_facts import in_scope, roadmap_focuses, target_focuses

# ============================================================================
# Counters — existing integer files plus atomic rebase progress checkpoints.
# ============================================================================


class RebaseBudgetUnavailable(ValueError):
    """Existing retry evidence could not be read safely."""


class Counters:
    def __init__(self, cfg: Config):
        self.state = cfg.state

    def read(self, name: str) -> int:
        try:
            txt = (self.state / name).read_text().strip()
        except FileNotFoundError:
            return 0
        except (OSError, UnicodeError):
            if name.startswith("rebase-pr-"):
                raise RebaseBudgetUnavailable("rebase counter is unreadable") from None
            return 0
        if (not txt.isascii() or not txt.isdigit()) and name.startswith("rebase-pr-"):
            raise RebaseBudgetUnavailable("rebase counter is malformed")
        return int(txt) if txt.isdigit() else 0

    def write(self, name: str, value: int) -> None:
        self._atomic_write(name, str(value))

    def _atomic_write(self, name: str, text: str) -> None:
        self.state.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", dir=self.state, prefix=f".{name}-", delete=False) as target:
                temporary = Path(target.name)
                target.write(text)
                target.flush()
                os.fsync(target.fileno())
            os.replace(temporary, self.state / name)
            directory = os.open(self.state, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass

    def incr(self, name: str) -> int:
        v = self.read(name) + 1
        self.write(name, v)
        return v

    def foreign_repair_launched(self, pr: int) -> bool:
        # An unreadable/malformed receipt still consumes the optional canary.
        return (self.state / f"rebase-foreign-launch-{pr}.json").exists()

    def record_foreign_repair_launch(self, pr: int, head: str, evidence: str, main: str) -> None:
        self._atomic_write(
            f"rebase-foreign-launch-{pr}.json",
            json.dumps({"pr": pr, "head": head, "evidence_main": evidence, "integration_main": main}),
        )

    def _rebase_progress(self, pr: int) -> dict | None:
        try:
            value = json.loads((self.state / f"rebase-progress-{pr}.json").read_text())
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as error:
            raise RebaseBudgetUnavailable("rebase checkpoint is unreadable") from error
        if not isinstance(value, dict) or value.get("schema") != "tauceti-rebase-progress/v1":
            raise RebaseBudgetUnavailable("rebase checkpoint has an unsupported schema")
        checkpoint = value.get("checkpoint")
        if not isinstance(checkpoint, dict) or not isinstance(checkpoint.get("head"), str):
            raise RebaseBudgetUnavailable("rebase checkpoint has invalid evidence")
        at = checkpoint.get("build_status_at")
        if not checkpoint["head"] or (at is not None and (type(at) is not int or at <= 0)):
            raise RebaseBudgetUnavailable("rebase checkpoint has invalid evidence")
        return {"head": checkpoint["head"]}

    def rebase_budget_key(self, pr: int) -> str:
        checkpoint = self._rebase_progress(pr)
        if checkpoint is None:
            return f"rebase-pr-{pr}"
        proof = json.dumps(checkpoint, sort_keys=True, separators=(",", ":"))
        epoch = hashlib.sha256(proof.encode()).hexdigest()[:20]
        return f"rebase-pr-{pr}-{epoch}"

    def observe_rebase_progress(self, patch: PRInfo, *, handoff: bool) -> None:
        # A successful process, comment, push, unknown mergeability or ongoing
        # reconciliation request is insufficient. Reuse exact-head CI and
        # mergeability evidence already obtained by the ordinary survey.
        if (
            patch.is_draft
            or handoff
            or patch.mergeable != "MERGEABLE"
            or not patch.build_success
            or not patch.head_oid
            or {name.lower() for name in patch.labels}
            & {"needs-rebase", "keep", "hold", "wip", "human", "on-hold", "do-not-close"}
        ):
            return
        at = patch.build_status_at
        if at is not None and (type(at) is not int or at <= 0):
            return
        checkpoint = {"head": patch.head_oid}
        if self._rebase_progress(patch.number) == checkpoint:
            return
        try:
            self._atomic_write(
                f"rebase-progress-{patch.number}.json",
                json.dumps({"schema": "tauceti-rebase-progress/v1", "checkpoint": checkpoint}),
            )
        except OSError:
            # If the checkpoint cannot be persisted, retain the existing budget.
            return


# ============================================================================
# Survey — the shared read-only core. Classifies all open PRs per work-kind
# without acting. The picker, `status`, and the TUI all consume this one object.
# ============================================================================

BUILD_FAIL = {"FAILURE", "ERROR", "TIMED_OUT", "CANCELLED", "STARTUP_FAILURE", "ACTION_REQUIRED"}

TARGET_MARKER_RE = re.compile(r"<!--tauceti-target:v1 (\{[^}]*\})-->")

TARGET_ID_RE = re.compile(r'"id"\s*:\s*"([^"]+)"')


def target_marker_focuses(body: str) -> tuple[str, ...]:
    """Concrete roadmap focuses in target markers; all/auto are scopes, not roadmap area names."""
    return tuple(target_focuses(body))


@dataclass(frozen=True)
class PRInfo:
    number: int
    head_oid: str
    head_ref: str
    head_owner: str
    head_repo: str
    is_draft: bool
    mergeable: str  # MERGEABLE | CONFLICTING | UNKNOWN
    author: str
    build_success: bool
    build_failed: bool
    author_is_bot: bool = False  # a GitHub App / bot author (e.g. the review bot's bump PRs)
    title: str = ""
    target_focuses: tuple[str, ...] = ()  # synchronous fallback while the derived roadmap label is pending
    labels: tuple[str, ...] = ()  # label names carried by the PR (the status pipeline + roadmap area)
    # When the authoritative `build` status was posted for THIS head (epoch seconds), i.e. the instant
    # the PR became reviewable — so "awaiting review since" is exactly this, and a new push resets it
    # with the head's new status. None when no `build` status carries a readable timestamp. Read off
    # the rollup we already fetch, so it costs no extra GitHub call.
    build_status_at: int | None = None
    # GitHub's own clock on the PR, carried so ReviewState can tell whether anything about it has
    # changed since it last read its comments (see ReviewState.observe). Free in the query we already
    # make; "" when unknown, which reads as "cannot tell" and falls back to the plain TTL.
    updated_at: str = ""
    bump_integration_failed: bool = False

    @property
    def automatic_bump(self) -> bool:
        return (
            self.author_is_bot
            and f"{self.head_owner}/{self.head_repo}" == TAUCETI
            and self.head_ref.startswith(BUMP_HEAD_PREFIX)
        )

    @staticmethod
    def from_json(d: dict) -> PRInfo:
        rollup = d.get("statusCheckRollup") or []
        head_owner = (d.get("headRepositoryOwner") or {}).get("login", "")
        # The required `build` signal is a commit STATUS (a StatusContext with context=="build",
        # carrying `state`), posted by the trusted sandboxed-build workflow — that is exactly what
        # branch protection and the merge gate read. We read ONLY that status, never a check-run. A
        # check-run reflects a JOB's outcome, which can go red on a transient INFRA / status-report
        # hiccup while the authoritative `build` status is green — the false-red that once routed a
        # green PR to fix-ci and wedged it. (The sandboxed-build job used to be named `build`, so its
        # check-run collided with this status context; TauCeti#1156 renamed it to `sandboxed-build`, so
        # no check-run named `build` exists at all now.) A PR with no `build` status yet is pending —
        # neither success nor failed — and simply waits for the trusted build to post.
        build_states = [c.get("state") for c in rollup if c.get("context") == "build"]
        # `gh` normalizes a StatusContext's createdAt to `startedAt`, so this is when the build status
        # was posted. With several `build` contexts the LATEST is when the head became fully green.
        posted = [_parse_iso8601(c.get("startedAt")) for c in rollup if c.get("context") == "build"]
        return PRInfo(
            number=d["number"],
            title=d.get("title", ""),
            target_focuses=tuple(d["target_focuses"])
            if "target_focuses" in d
            else target_marker_focuses(d.get("body", "")),
            head_oid=d.get("headRefOid", ""),
            head_ref=d.get("headRefName", ""),
            head_owner=head_owner,
            head_repo=(d.get("headRepository") or {}).get("name", ""),
            is_draft=bool(d.get("isDraft")),
            mergeable=d.get("mergeable", "UNKNOWN"),
            author=(d.get("author") or {}).get("login", ""),
            author_is_bot=bool((d.get("author") or {}).get("is_bot")),
            build_success=bool(build_states) and all(s == "SUCCESS" for s in build_states),
            build_failed=any(s in BUILD_FAIL for s in build_states),
            labels=tuple((lb.get("name") or "") for lb in (d.get("labels") or [])),
            build_status_at=max([t for t in posted if t is not None], default=None),
            updated_at=str(d.get("updatedAt") or ""),
            bump_integration_failed=any(
                c.get("context") == "bump-integration" and c.get("state") in BUILD_FAIL for c in rollup
            ),
        )


@dataclass
class Candidate:
    pr: int
    head: str
    reason: str = ""
    attempts: int = 0
    budget: int = 0
    contest: str = ""  # set to the contested rubric when this is an author-contest re-review
    contest_reply_id: int = 0  # the review-comment id of the contesting reply (the 👀 claim anchor)
    ready_at: int | None = None  # when this exact review unit became actionable
    preferred_reviewer: str = ""  # latest scoreboard publisher; gets a short first refusal
    rebase_budget_key: str = ""  # immutable budget chosen by this survey, including its verified progress epoch
    foreign_repair_base: str = ""  # explicit one-PR operator scope; never adds the fork to other stages
    bump_integration_failure: bool = False


@dataclass
class WorkKind:
    name: str
    actionable: list[Candidate] = field(default_factory=list)
    suppressed: list[Candidate] = field(default_factory=list)

    @property
    def count(self) -> int:
        return len(self.actionable)


@dataclass
class Survey:
    worker_id: str
    open_prs: list[PRInfo] = field(default_factory=list)
    n_open_nondraft: int = 0
    n_reviewable: int = 0
    # Open non-draft PRs bucketed by the STATUS_LABELS pipeline, in that fixed order: (label, total,
    # mine) where `mine` is the subset authored by the worker's own GitHub identity. Drives the
    # per-round "open PRs" line; a zero bucket is still listed so the columns line up round to round.
    status_labels: list[tuple[str, int, int]] = field(default_factory=list)
    # Open non-draft PRs carrying NONE of the STATUS_LABELS — normally zero (CI keeps every PR
    # labelled). Surfaced on the line only when nonzero, so a stalled labeller (which would otherwise
    # drain every bucket to zero and read as "no open PRs") shows up instead of vanishing.
    n_status_unlabeled: int = 0
    rebaseable: WorkKind = field(default_factory=lambda: WorkKind("rebase"))
    reviewable: WorkKind = field(default_factory=lambda: WorkKind("review"))
    needs_fix: WorkKind = field(default_factory=lambda: WorkKind("fix"))
    red_ci: WorkKind = field(default_factory=lambda: WorkKind("fix-ci"))
    bump: WorkKind = field(default_factory=lambda: WorkKind("bump"))  # broken bump-mathlib PRs
    lint_repair: WorkKind = field(default_factory=lambda: WorkKind("lint-repair"))  # full-lint repair PRs
    progress: WorkKind = field(default_factory=lambda: WorkKind("progress"))  # a roadmap report is due
    overlap_scan: WorkKind = field(default_factory=lambda: WorkKind("overlap-scan"))
    overlap_fix: WorkKind = field(default_factory=lambda: WorkKind("overlap-fix"))
    roadmap_only: str = ""
    roadmap_skip: list[str] = field(default_factory=list)
    # This is deliberately scoped by roadmap_only/roadmap_skip: authoring backpressure in a focused
    # run is the number of our open, non-draft roadmap PRs that belong to that run's selected areas,
    # not the number of every PR we have open across the project.
    n_mine_open: int = 0
    roadmap_backpressure: bool = False
    max_open_prs: int = field(default_factory=configured_max_open_prs)
    next_auto_stage: str | None = None
    github_failed: bool = False
    discovery_gaps: int = 0  # cached histories needed before ruling out global maintenance
    errors: list[str] = field(default_factory=list)
    # PRs whose review keeps ERRORING (engine can't post a verdict) past MAX_REVIEW_ERRORS: the worker
    # can't review them and CI's round cap can't catch them (rounds never advance), so each round
    # escalates — a loud warning + a tracking issue — rather than stranding them silently.
    review_stuck: list[int] = field(default_factory=list)
    # Heads a peer reviewer is actively reviewing right now (an unexpired in-progress marker on the
    # exact head). Skipped this round so the worker neither pays the engine's launch cost just to have
    # it skip nor busy-loops on the one PR a peer holds. (pr, providers) for a one-line status note.
    review_inflight: list[tuple[int, str]] = field(default_factory=list)
    # Tended PRs that are NOT actionable for `fix`, each with a one-line reason (awaiting first review,
    # head moved since review, reviews all green, fix attempts spent, or a transient fetch failure). A
    # fix-focused worker logs these so it explains its idleness instead of sitting on a bare "no eligible
    # work" — reviews are async, so a one-shot `work --only fix` right after opening a PR commonly finds
    # the scoreboard not yet posted. (pr, reason) for a one-line status note.
    fix_waiting: list[tuple[int, str]] = field(default_factory=list)

    def __post_init__(self) -> None:
        # Retained only so the TUI's live roadmap dials can rescope without another GitHub fetch.
        # This is intentionally not a dataclass field: `status --json` uses asdict() and should not
        # duplicate the worker's PRs in its public payload.
        self._mine_open_prs: list[PRInfo] = []
        # pr -> why the (undocumented) review throttles removed it from THIS round's queue. Also not a
        # dataclass field, and for a second reason beyond the payload: it is a property of one round's
        # options, not of the survey, so `status` and the dashboard must never report it. It exists so
        # a `--pr` round can explain a target the throttles took, instead of reporting the survey found
        # it no work.
        self._review_throttled: dict[int, str] = {}

    def kind(self, name: str) -> WorkKind:
        return {
            "rebase": self.rebaseable,
            "review": self.reviewable,
            "fix": self.needs_fix,
            "fix-ci": self.red_ci,
            "bump": self.bump,
            "lint-repair": self.lint_repair,
            "progress": self.progress,
            "overlap-scan": self.overlap_scan,
            "overlap-fix": self.overlap_fix,
        }[name]

    def rescope_roadmap(self) -> None:
        """Recompute authoring pressure after roadmap-only/skip changes, including live TUI dials."""
        automatic = getattr(self, "_automatic_open_count", None)
        self.n_mine_open = (
            automatic
            if automatic is not None
            else roadmap_open_count(self._mine_open_prs, self.roadmap_only, self.roadmap_skip)
        )
        self.roadmap_backpressure = self.n_mine_open >= self.max_open_prs
        self.next_auto_stage = _next_auto_stage(self)

    def status_label_line(self) -> str:
        """One-line breakdown of open non-draft PRs by status label: 'N label (M mine), N label (M),
        ...'. Each entry pairs the total carrying that label with the subset the worker itself authored;
        the first entry spells out 'mine' so the trailing parenthesized numbers read unambiguously. A
        PR may carry several status labels (counted in each), so the totals need not sum to the PR
        count; a nonzero unlabeled tail flags PRs the labeller missed."""
        parts = []
        for i, (label, total, mine) in enumerate(self.status_labels):
            parts.append(f"{total} {label} ({mine} mine)" if i == 0 else f"{total} {label} ({mine})")
        if self.n_status_unlabeled:
            parts.append(f"{self.n_status_unlabeled} unlabeled")
        return ", ".join(parts)


def bucket_status_labels(nondraft: list[PRInfo], me_login: str) -> tuple[list[tuple[str, int, int]], int]:
    """Bucket open non-draft PRs by the STATUS_LABELS pipeline (fixed order). Returns
    (buckets, unlabeled) where each bucket is (label, total, mine) — total carrying that label, and the
    subset authored by `me_login` — and `unlabeled` counts PRs with none of the status labels. A PR
    with several status labels is counted in each bucket, so the totals need not partition the PRs.
    Shared by survey() and its test so there is one bucketing rule, not two."""
    buckets = [
        (
            label,
            sum(1 for p in nondraft if label in p.labels),
            sum(1 for p in nondraft if label in p.labels and p.author == me_login),
        )
        for label in STATUS_LABELS
    ]
    unlabeled = sum(1 for p in nondraft if not any(label in p.labels for label in STATUS_LABELS))
    return buckets, unlabeled


def roadmap_open_count(prs: list[PRInfo], only: str, skip: list[str]) -> int:
    """Count open roadmap PRs in the current authoring scope.

    ``only`` is the survey's normalized value: a concrete area, ``auto`` (a random eligible area
    will be selected later), or ``any`` (all eligible areas). For a concrete area, only its exact
    ``roadmap/<area>`` focus counts and --roadmap-only wins over an overlapping skip. Derived area
    labels are authoritative when present; a concrete synchronous target-marker focus
    is the fallback during label lag. ``roadmap/none`` is not roadmap work, while an Unknown label
    or an unresolved ``roadmap/`` branch counts in every scope so the safety limit cannot fail open.
    A PR is counted once.
    """

    def focuses(p: PRInfo):
        return roadmap_focuses(p.labels, p.target_focuses, p.head_ref)

    if only not in ("", "any", "auto"):
        return sum((areas := focuses(p)) is None or only in areas for p in prs)

    skipped = set(skip)
    return sum((areas := focuses(p)) is None or bool(areas - skipped) for p in prs)


def spread_candidates(candidates: list, rng=random) -> list:
    """Return a stage's candidates in a randomized order so several workers starting together don't all
    converge on the same (lowest-numbered) PR and collide. The survey has already dropped work a peer is
    KNOWN to hold; this only varies which of the remaining, apparently-free PRs each worker tries first —
    turning systematic collisions (every worker picks the lowest, discovers the clash, repeats) into rare,
    self-correcting ones. Pure WORKER-side work-allocation: the real de-contention is unchanged — the
    review engine's in-progress marker for reviews, the branch claim for fix/fix-ci/rebase — this just
    spreads the first pick. Each worker is its own process with independent RNG state, so concurrent
    workers shuffle differently. Returns a new list."""
    out = list(candidates)
    rng.shuffle(out)
    return out


def _review_age_weight(candidate: Candidate, now: float) -> float:
    """Linear aging with a one-day cap; unknown/future timestamps stay at the base weight."""
    waited = max(0.0, now - candidate.ready_at) if candidate.ready_at is not None else 0.0
    return 1.0 + min(waited, REVIEW_AGE_CAP_S) / REVIEW_AGE_SCALE_S


def _scoreboard_reviewer(meta: Meta) -> str:
    value = meta.data.get("submitted_by")
    return value if isinstance(value, str) else ""


def prioritize_review_candidates(
    candidates: list[Candidate], reviewer: str, *, now: float | None = None, rng=random
) -> tuple[list[Candidate], list[Candidate]]:
    """Apply soft reviewer affinity, then return an age-weighted random permutation.

    During the grace period a prior publisher's units form that reviewer's first tier and are hidden
    from peers. Expired or unowned units form the shared tier. Missing identity/timestamps fail open.
    The second return value is the foreign-affinity set deferred for this worker.
    """
    stamp = time.time() if now is None else now
    login = (reviewer or "").casefold()
    owned: list[Candidate] = []
    shared: list[Candidate] = []
    deferred: list[Candidate] = []
    for candidate in candidates:
        owner = (candidate.preferred_reviewer or "").strip()
        waited = max(0.0, stamp - candidate.ready_at) if candidate.ready_at is not None else None
        in_grace = bool(owner and login and waited is not None and waited < REVIEW_AFFINITY_GRACE_S)
        if not in_grace:
            shared.append(candidate)
        elif owner.casefold() == login:
            owned.append(candidate)
        else:
            deferred.append(candidate)

    def weighted(items: list[Candidate]) -> list[Candidate]:
        # Exponential keys produce a weighted permutation without replacement. random() may legally
        # return zero, so clamp it away from log(0) without changing any ordinary RNG result.
        keyed = []
        for candidate in items:
            draw = max(float(rng.random()), 1e-300)
            keyed.append((-math.log(draw) / _review_age_weight(candidate, stamp), candidate))
        keyed.sort(key=lambda item: item[0])
        return [candidate for _, candidate in keyed]

    return weighted(owned) + weighted(shared), deferred


def fix_disposition(
    meta: Meta,
    head: str,
    build_success: bool,
    blocking: bool,
    per_head: int,
    *,
    pending_contest: bool = False,
    unlimited: bool = False,
) -> tuple[str, str]:
    """Classify a tended PR for the `fix` stage from its scoreboard meta. Returns (disposition, reason):

      'actionable' — a blocking rubric stands at the current head, under the per-head attempt budget
      'exhausted'  — blocking at head, but the per-head fixer budget is spent (reason names the count)
      'waiting'    — not actionable now; reason explains why (awaiting first review, head moved,
                     reviews all green, or a transient fetch failure) so a fix-focused worker can say
                     whether to wait for reviews, re-push, or stop
      'skip'       — nothing worth a status line (a red PR awaiting CI, not review)

    `blocking` is rs.ledger_blocking(pr, head) — the same authority CI's close reads — passed in so this
    stays a pure formatter with no second copy of the blocking rule. A pure function (no I/O): the survey
    fetches the meta + predicate, this decides the disposition and phrases the reason.
    """
    lh = str(meta.data.get("head_sha") or "")
    if lh != head and not build_success:
        return ("skip", "")  # red build: fix-ci/bump greens it before a review can land — not fix's
    # A failed live fetch — whether or not a stale cache backs it — means we can't trust head_sha to
    # tell "head moved" from "couldn't refresh", so don't assert either; say so and let a later round
    # retry. This check used to sit inside the head-moved branch alone, which left the more dangerous
    # case uncovered: a stale scoreboard whose head HAPPENS to match the current one sailed straight
    # through to 'actionable' and scheduled a fixer off review state nobody had been able to confirm.
    # `assumed` is deliberately not refused here. It is the normal state of a warm cache, and refusing
    # it would park every fix candidate until the backstop expired; what protects the spend is that
    # dispatch() re-reads this PR live before acting on it.
    if meta.provenance in ("fetch_failed", "stale"):
        return ("waiting", "could not read current review state (GitHub fetch failed) — will retry next round")
    if lh != head:
        # No (current) review verdict stands at the head.
        if lh:
            return ("waiting", f"reviewed at {lh[:12]}; head moved to {head[:12]} — awaiting re-review")
        return ("waiting", "build-green, awaiting first review (no scoreboard at this head yet)")
    if not blocking:
        # head matches the scoreboard. With verdicts present this is a genuine all-green; with none at all
        # (a malformed/skeleton scoreboard) ledger_blocking is also false, but "all green" would mislead.
        if (meta.data.get("states") or {}) or (meta.data.get("runs") or []):
            return ("waiting", "reviews all green — nothing to fix")
        return ("waiting", "review recorded at this head but no rubric verdicts yet — awaiting review")
    if pending_contest:
        # A fix round may legitimately answer a wrong finding by contesting it without pushing. Until
        # review adjudicates that reply, the durable scoreboard remains blocking at the same head;
        # scheduling another fixer would only burn the per-head budget on the identical finding.
        return ("waiting", "author contest awaiting re-review")
    if not unlimited and per_head >= MAX_FIX_ATTEMPTS:
        return (
            "exhausted",
            f"blocking review at head, but fix attempts are spent ({per_head}/{MAX_FIX_ATTEMPTS}) — needs a human",
        )
    return ("actionable", "")


def progress_argv(state: Path, *args: str) -> list[str]:
    """The TauCetiProgress CLI, cached separately for each immutable source revision.

    uv's shared ``uvx`` tool environment is keyed by the unchanged package name/version rather than
    reliably by the Git revision passed through ``--from``. Reusing it after a pin bump can therefore
    execute an older checkout. A per-ref cache preserves normal reuse within a release while making
    the revision part of the cache identity.
    """
    cache = state / "cache" / "uvx" / "tauceti-progress" / PROGRESS_REF
    return [
        "uvx",
        "--cache-dir",
        str(cache),
        "--from",
        f"git+https://github.com/{PROGRESS}@{PROGRESS_REF}",
        "tauceti-progress",
        *args,
    ]


def progress_due(cfg: Config, counters: Counters) -> tuple[bool, str]:
    """Is a roadmap progress report due? `(due, reason)`.

    Cost discipline matters here, because this runs on EVERY round and every ~90s behind the
    dashboard, whereas a report is wanted about every eight hours. So this is the cheap half of the
    decision: one `gh api` call for the roadmap repo's recent commit subjects, no clone, no PR query
    — and the VERDICT is cached (not the raw response), so it is not re-derived while a round is in
    flight. The expensive half (the roadmap checkout, the per-area label queries, the model) happens
    in the round.

    Never raises. The cascade is first-actionable-wins and a stage that raises aborts the whole round,
    so a broken `uvx`, a GitHub hiccup or a bad exit here must read as "not due" and let the round fall
    through to review and fix work. A stage that can throw is a stage that can wedge the worker.
    """
    cache = cfg.state / "cache" / "progress-due.json"
    try:
        if cache.exists() and (time.time() - cache.stat().st_mtime) < PROGRESS_TTL:
            d = json.loads(cache.read_text())
            return bool(d.get("due")), str(d.get("reason") or "")
    except (OSError, ValueError):
        pass

    # A durable attempt breaker, independent of whether a report ever LANDS. The cadence check keys on
    # the last merged report, so a stuck or rejected PR would otherwise leave it true for ever and
    # every round would burn on it.
    last_attempt = counters.read("progress-attempt-ts")
    if last_attempt and (time.time() - last_attempt) < PROGRESS_ATTEMPT_GAP:
        gap_h = (time.time() - last_attempt) / 3600.0
        return False, f"a progress round was attempted {gap_h:.1f}h ago; waiting out the attempt gap"
    if counters.read(PROGRESS_ERROR_KEY) >= MAX_PROGRESS_ERRORS:
        return False, (
            f"progress rounds have failed {counters.read(PROGRESS_ERROR_KEY)}x; "
            f"backing off (clear state/{PROGRESS_ERROR_KEY} to retry)"
        )

    try:
        proc = subprocess.run(progress_argv(cfg.state, "due"), capture_output=True, text=True, timeout=300)
        # The verdict is on stdout. stderr carries uvx's build chatter ("Updating ...", "Building
        # ..."), which is not part of the reason and would otherwise fill the dashboard cell.
        out = (proc.stdout or "").strip().splitlines()
        verdict = out[-1].strip() if out else ""
        if proc.returncode == 0:
            due, reason = True, verdict or "due"
        elif proc.returncode == EX_NOPROGRESS:
            due, reason = False, verdict or "not due"
        else:
            err = (proc.stderr or "").strip().splitlines()
            detail = verdict or (err[-1].strip() if err else "")
            due, reason = False, f"progress due-check failed (rc={proc.returncode}): {detail[:200]}"
    except (OSError, subprocess.SubprocessError) as exc:
        due, reason = False, f"progress due-check could not run: {exc}"

    try:
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps({"due": due, "reason": reason}))
    except OSError:
        pass
    return due, reason


def bust_progress_cache(cfg: Config) -> None:
    """Drop the cached `due` verdict so the next survey re-derives it.

    Called the moment a report is opened: without it this same worker would still read `due` from
    cache on its next round, minutes later, and open a second report before the first one merged.
    """
    try:
        (cfg.state / "cache" / "progress-due.json").unlink(missing_ok=True)
    except OSError:
        pass


def survey(
    cfg: Config,
    gh: GitHub,
    rs: ReviewState,
    counters: Counters,
    *,
    deep: bool = True,
    stages=None,
    selected_roadmap=None,
    numbers=(),
    count_all_roadmaps=False,
    cached_only=False,
) -> Survey:
    """Classify relevant open PRs per enabled work-kind, without writes or model work.

    Local cache and verified rebase-progress checkpoints may be updated.

    `deep=False` skips the per-PR scoreboard reads (faster, coarse) for a quick glance; the picker
    always uses deep=True.
    """
    began = time.monotonic()
    _f = selected_roadmap if selected_roadmap is not None else roadmap_only()
    enabled = set(AUTO_STAGES if stages is None else stages)
    # Automatic rounds discover existing work across all areas. Explicit scopes
    # still filter before history reads; authoring chooses its area after maintenance.
    sv = Survey(
        worker_id=cfg.wid,
        roadmap_only=("auto" if _f is None else (_f or "any")),
        roadmap_skip=roadmap_skip(),
    )
    compact = []
    if seed := getattr(rs, "seed_survey", None):
        seed()
    try:
        from .github_state import read_survey

        available, compact = (
            read_survey(
                gh.repo,
                sv.roadmap_only,
                sv.roadmap_skip,
                numbers=numbers,
                stages=[] if cached_only else sorted(enabled),
            )
            if deep and hasattr(gh, "repo")
            else (False, [])
        )
        if cached_only and not available:
            sv.github_failed = True
            sv.errors.append("global maintenance cache unavailable; waiting for the shared GitHub state service")
            return sv
        raw = [row["pr"] for row in compact] if available else gh.open_prs()
        if available and seed:
            seed(compact)
    except GitHubError as e:
        sv.github_failed = True
        sv.errors.append(str(e))
        return sv
    prs = [PRInfo.from_json(d) for d in raw]
    if count_all_roadmaps:
        counts = getattr(compact, "author_counts", None)
        all_prs = prs if not available else ([] if counts is not None else [PRInfo.from_json(d) for d in gh.open_prs()])
        sv._automatic_open_count = (
            counts.get(me(), 0)
            if counts is not None
            else roadmap_open_count(
                [p for p in all_prs if not p.is_draft and p.author == me()], "auto", sv.roadmap_skip
            )
        )
    if deep:
        prs = [
            p
            for p in prs
            if (
                p.number in numbers
                if numbers
                else in_scope(p.labels, p.target_focuses, p.head_ref, sv.roadmap_only, sv.roadmap_skip)
            )
        ]
    sv.open_prs = prs
    # Hand ReviewState this pass's clocks BEFORE any per-PR read below: they are what let it skip the
    # comment fetch for a PR that has not moved since the last round looked at it. A shallow survey
    # reads no review state at all and its callers need not supply one.
    if rs is not None:
        rs.observe(prs)
    nondraft = [
        p
        for p in prs
        if not p.is_draft
        and not (
            p.automatic_bump
            and {label.lower() for label in p.labels} & {"keep", "hold", "wip", "human", "on-hold", "do-not-close"}
        )
    ]
    me_login = me()
    mine = [p for p in nondraft if p.author == me_login]
    # Tend our own PRs, plus bot PRs hosted on canonical when this identity can push there. Only query
    # that permission while such a bot PR is open; an unknown result skips optional bot work this round.
    bot_on_canonical = bool(enabled & {"rebase", "fix", "fix-ci", "bump", "lint-repair"}) and any(
        p.author_is_bot and p.head_owner == TAUCETI_OWNER for p in nondraft
    )
    tend_bot = bot_on_canonical and can_push(TAUCETI) is True
    tended = [
        p
        for p in nondraft
        if (p.author == me_login or (tend_bot and p.author_is_bot and p.head_owner == TAUCETI_OWNER))
        and not {label.lower() for label in p.labels} & {"keep", "hold", "wip", "human", "on-hold", "do-not-close"}
    ]
    facts = {row["pr"]["number"]: row["review"] for row in compact or []}
    gaps: set[int] = set()

    def history_ready(p):
        if not cached_only:
            return True
        covered = facts.get(p.number, {})
        ready = covered.get("issue_complete") and covered.get("review_complete")
        if not ready:
            gaps.add(p.number)
        return ready

    sv.n_open_nondraft = len(nondraft)
    sv.n_reviewable = sum(1 for p in nondraft if p.build_success)
    sv._mine_open_prs = mine
    sv.rescope_roadmap()
    # Bucket open non-draft PRs by the STATUS_LABELS pipeline (fixed order), pairing each label's
    # total with the subset this identity authored, for the per-round "open PRs" line.
    sv.status_labels, sv.n_status_unlabeled = bucket_status_labels(nondraft, me_login)

    # 1) rebase: tended (ours or bot-authored), conflicting or a sweep handoff for
    #    this exact head, under the per-PR budget since last verified progress.
    #    Covers a bot bump PR that main moved out from under — no bump-specific conflict resolver
    #    exists, so rebase owns the git conflict on those too. No review-round gate: a conflicting PR
    #    is rebased until it merges or CI retires it.
    foreign_repairs = []
    if "rebase" in enabled:
        from .repair_route import authorize, target

        scope = target()
        for p in nondraft if scope else []:
            if p not in tended and (p.number, p.head_oid) == (scope.pr, scope.head):
                if integration := authorize(gh, p.number, p.head_oid, p.head_ref, f"{p.head_owner}/{p.head_repo}"):
                    foreign_repairs.append(p)
    for p in tended + foreign_repairs if "rebase" in enabled else []:
        foreign = p in foreign_repairs
        labels = {label.lower() for label in p.labels}
        if labels & {"keep", "hold", "wip", "human", "on-hold", "do-not-close"}:
            continue
        if (
            cached_only
            and p.mergeable != "CONFLICTING"
            and "needs-rebase" in labels
            and not facts.get(p.number, {}).get("issue_complete")
        ):
            gaps.add(p.number)
            continue
        handoff = foreign or (
            p.mergeable != "CONFLICTING"
            and "needs-rebase" in labels
            and (
                p.head_oid in rs._survey_facts[p.number]["rebase_heads"]
                if getattr(rs, "_survey_facts", {}).get(p.number, {}).get("issue_complete")
                else gh.rebase_requested(p.number, p.head_oid)
            )
        )
        # Healthy tended PRs must be observed too: they replenish the budget
        # before a later main change makes them conflicting again. Test doubles
        # without persistent state retain the legacy counter interface.
        if p.automatic_bump:
            # A red dependency migration may require many conflict repairs before
            # its first green head. Existing loop backoff and quotas bound retries.
            if p.mergeable == "CONFLICTING" or handoff:
                sv.rebaseable.actionable.append(
                    Candidate(
                        p.number,
                        p.head_oid,
                        "dependency bump reconciliation",
                        rebase_budget_key=f"rebase-bump-{p.number}",
                    )
                )
            continue
        budget_error = None
        try:
            if observe := getattr(counters, "observe_rebase_progress", None):
                observe(p, handoff="needs-rebase" in labels)
        except RebaseBudgetUnavailable as error:
            budget_error = str(error)
        reason = "conflicting"
        if p.mergeable != "CONFLICTING":
            if not handoff:
                continue
            reason = "authenticated request for integration repair"
        c = Candidate(p.number, p.head_oid, reason)
        if foreign:
            c.foreign_repair_base = integration
            c.reason = "explicitly scoped foreign-fork integration repair"
        try:
            if budget_error:
                raise RebaseBudgetUnavailable(budget_error)
            key_for = getattr(counters, "rebase_budget_key", None)
            # A handoff can start from an already green, mergeable PR. Another
            # green push cannot prove it succeeded; retain its per-PR cap.
            c.rebase_budget_key = key_for(p.number) if key_for and not handoff else f"rebase-pr-{p.number}"
            c.attempts = counters.read(c.rebase_budget_key)
            if foreign and getattr(counters, "foreign_repair_launched", lambda _: False)(p.number):
                c.attempts = max(1, c.attempts)
        except RebaseBudgetUnavailable as error:
            c.reason = f"{reason}; {error}"
            c.attempts = MAX_REBASE_ATTEMPTS
        c.budget = 1 if foreign else MAX_REBASE_ATTEMPTS
        (sv.rebaseable.suppressed if c.attempts >= c.budget else sv.rebaseable.actionable).append(c)

    # 2) review: non-draft, build-green. Eligible when the head is NOT cleanly reviewed (a new commit
    #    or an errored round → normal review; no round budget here, CI retires a non-converging PR),
    #    OR a fresh author CONTEST reply landed since the last review at a clean head (→ contest path,
    #    bounded by the contest caps). The only worker-side stop is the review-ERROR cap: a PR whose
    #    review keeps erroring without completing a review is escalated, not silently dropped.
    for p in nondraft if "review" in enabled else []:
        if {label.lower() for label in p.labels} & {"keep", "hold", "wip", "human", "on-hold", "do-not-close"}:
            continue
        if not p.build_success:
            continue
        if not deep:
            sv.reviewable.actionable.append(
                Candidate(p.number, p.head_oid, "build-green, head not cleanly reviewed", ready_at=p.build_status_at)
            )
            continue
        if not history_ready(p):
            continue
        m = rs.gh_meta(p.number)
        if rs.ledger_clean_head(p.number) != p.head_oid:
            # normal review path: the head moved or the last round errored.
            c = Candidate(
                p.number,
                p.head_oid,
                "build-green, head not cleanly reviewed",
                attempts=counters.read(f"review-err-{p.number}"),
                budget=MAX_REVIEW_ERRORS,
                ready_at=p.build_status_at,
                preferred_reviewer=_scoreboard_reviewer(m),
            )
            if not p.automatic_bump and c.attempts >= c.budget:
                sv.reviewable.suppressed.append(c)
                sv.review_stuck.append(p.number)  # can't be reviewed → escalate (warn + issue)
                continue
            # A peer reviewer holds this exact head (de-contention is on the head alone): skip it now,
            # the same call the engine's coordinate() would make after a full build+launch. Doing it
            # here keeps the loop off the one PR a peer is reviewing instead of re-selecting it every
            # round and spending ~25s per pass to have the engine skip. Fail-open (a fetch failure
            # reads as 'not held'); the engine's own claim is still the authoritative backstop.
            cov = rs.inflight_review(p.number, p.head_oid)
            if cov:
                sv.review_inflight.append((p.number, ",".join(sorted(cov))))
                continue
            sv.reviewable.actionable.append(c)
            continue
        # Head is cleanly reviewed: only a NEW author contest reply re-opens it. The engine records
        # the highest reply id it has adjudicated as `replies_through` in the scoreboard meta, so a
        # reply with a higher id is one no review round has answered yet — precise (monotonic id),
        # with no second-resolution ambiguity, and self-clearing (the contest round advances
        # replies_through past it).
        reply = rs.newest_contest_reply(p.number)
        if not reply:
            continue
        through = m.data.get("replies_through")
        through = through if isinstance(through, int) else 0
        if reply["id"] <= through:
            continue  # already adjudicated by some review round
        # A 👀 on the contesting reply claims an in-flight re-review: it suppresses a re-fire in the
        # window before the new scoreboard lands, across the WHOLE fleet (the claim is on GitHub, not
        # in a per-worker store). A claim left by a crashed worker frees itself after CONTEST_CLAIM_TTL.
        # Global discovery never reads reactions for every contest. Dispatch
        # rechecks the selected reply's claim immediately before launching.
        age = None if cached_only else gh.fresh_claim_age(reply["id"])
        if age is not None and age < CONTEST_CLAIM_TTL:
            continue
        rubric = reply["rubric"]
        c = Candidate(
            p.number,
            p.head_oid,
            f"author contest on {rubric}",
            contest=rubric,
            contest_reply_id=reply["id"],
            attempts=counters.read(f"review-contest-{p.number}"),
            budget=MAX_REVIEW_CONTESTS,
            ready_at=_parse_iso8601(reply.get("created_at")),
            preferred_reviewer=_scoreboard_reviewer(m),
        )
        if not p.automatic_bump and (
            counters.read(f"review-contest-{p.number}") >= MAX_REVIEW_CONTESTS
            or counters.read(f"review-contest-{p.number}-{rubric}") >= MAX_REVIEW_CONTESTS_PER_RUBRIC
            or counters.read(f"review-err-{p.number}") >= MAX_REVIEW_ERRORS
        ):
            sv.reviewable.suppressed.append(c)
        else:
            sv.reviewable.actionable.append(c)

    if deep and "fix" in enabled:
        # 3) fix: tended (ours or bot-authored), reviewed-at-head, latest rubric blocking, under
        #    budgets. Bump PRs get reviewed like any other, so a blocking rubric on one is ours to fix
        #    (orthogonal to the bump stage, which only adapts a RED build). Every tended PR that is NOT
        #    actionable records a one-line reason in fix_waiting (awaiting first review, head moved, all
        #    green, attempts spent) so a fix-focused worker explains its idleness instead of a bare
        #    "no eligible work" — reviews are async, so a one-shot fix run can precede the scoreboard.
        for p in tended:
            if cached_only and (not p.build_success or not history_ready(p)):
                continue
            meta = rs.gh_meta(p.number)
            blocking = rs.ledger_blocking(p.number, p.head_oid)
            per_head = counters.read(f"fix-{p.number}-{p.head_oid[:12]}")
            pending_contest = False
            if blocking and str(meta.data.get("head_sha") or "") == p.head_oid:
                reply = rs.newest_contest_reply(p.number)
                through = meta.data.get("replies_through")
                through = through if isinstance(through, int) else 0
                pending_contest = bool(reply and reply["id"] > through)
            disp, why = fix_disposition(
                meta,
                p.head_oid,
                p.build_success,
                blocking,
                per_head,
                pending_contest=pending_contest,
                unlimited=p.automatic_bump,
            )
            if disp == "skip":
                continue
            if disp == "actionable":
                c = Candidate(
                    p.number, p.head_oid, "blocking review at head", attempts=per_head, budget=MAX_FIX_ATTEMPTS
                )
                sv.needs_fix.actionable.append(c)
                continue
            sv.fix_waiting.append((p.number, why))
            if disp == "exhausted":
                c = Candidate(
                    p.number, p.head_oid, "blocking review at head", attempts=per_head, budget=MAX_FIX_ATTEMPTS
                )
                sv.needs_fix.suppressed.append(c)

    # 4) fix-ci: tended (ours or bot-authored), build FAILED at head, under budgets. A red bump PR is
    #    the bump stage's job (its adaptation prompt knows mathlib moved), and a red lint-repair PR is
    #    the lint-repair stage's, so fix-ci defers those; it picks up only other red PRs (ours, or
    #    any other bot-authored one).
    for p in tended if "fix-ci" in enabled else []:
        if not p.build_failed or p.head_ref.startswith((BUMP_HEAD_PREFIX, LINT_REPAIR_HEAD_PREFIX)):
            continue
        c = Candidate(p.number, p.head_oid, "build failed at head")
        per_head = counters.read(f"ci-{p.number}-{p.head_oid[:12]}")
        per_pr = counters.read(f"ci-pr-{p.number}")
        c.attempts, c.budget = per_head, MAX_CI_ATTEMPTS
        if per_head >= MAX_CI_ATTEMPTS or per_pr >= MAX_CI_PR_ATTEMPTS:
            sv.red_ci.suppressed.append(c)
        else:
            sv.red_ci.actionable.append(c)

    # 5) bump: a bump-mathlib PR (opened by the review bot) whose build is RED — mathlib moved
    #    out from under the last-known-good bump and TauCeti/ needs adapting. We adapt it; we never
    #    author a bump (the bot owns opening them, CI owns merging the green ones). This is the
    #    bump-specific CI-fixer: fix-ci defers a red bump PR here (rebase still owns its conflicts and
    #    fix still owns its review findings).
    for p in tended if "bump" in enabled else []:
        integration = p.build_success and p.bump_integration_failed
        branch = p.head_ref.startswith(BUMP_HEAD_PREFIX) or (integration and p.head_ref == "hopscotch/lkg-bump")
        if not (branch and (p.build_failed or integration)):
            continue
        if {label.lower() for label in p.labels} & {
            "keep",
            "hold",
            "wip",
            "human",
            "on-hold",
            "do-not-close",
            "needs-rebase",
        }:
            continue
        c = Candidate(
            p.number,
            p.head_oid,
            "bump-mathlib, singleton integration failed" if integration else "bump-mathlib, build red",
            bump_integration_failure=integration,
        )
        per_head = counters.read(f"bump-{p.number}-{p.head_oid[:12]}")
        per_pr = counters.read(f"bump-pr-{p.number}")
        c.attempts, c.budget = per_head, MAX_BUMP_ATTEMPTS
        if not p.automatic_bump and (per_head >= MAX_BUMP_ATTEMPTS or per_pr >= MAX_BUMP_PR_ATTEMPTS):
            sv.bump.suppressed.append(c)
        else:
            sv.bump.actionable.append(c)

    # 5b) lint-repair: the repair PR TauCeti's daily full lint opens (branch lint-repair/..., authored
    #    by the review bot) whose build is RED — main carries environment-lint violations that PR
    #    builds, which lint only changed modules, could not see, and TauCeti/ needs fixing. Like bump:
    #    we repair it and never author one (the daily lint owns opening them, CI owns merging).
    for p in tended if "lint-repair" in enabled else []:
        if not (p.head_ref.startswith(LINT_REPAIR_HEAD_PREFIX) and p.build_failed):
            continue
        c = Candidate(p.number, p.head_oid, "lint-repair, build red")
        per_head = counters.read(f"lint-repair-{p.number}-{p.head_oid[:12]}")
        per_pr = counters.read(f"lint-repair-pr-{p.number}")
        # Report whichever budget is the binding one, so a lifetime-capped PR does not read as 0/3.
        if per_pr >= MAX_LINT_REPAIR_PR_ATTEMPTS:
            c.attempts, c.budget = per_pr, MAX_LINT_REPAIR_PR_ATTEMPTS
        else:
            c.attempts, c.budget = per_head, MAX_LINT_REPAIR_ATTEMPTS
        if per_head >= MAX_LINT_REPAIR_ATTEMPTS or per_pr >= MAX_LINT_REPAIR_PR_ATTEMPTS:
            sv.lint_repair.suppressed.append(c)
        else:
            sv.lint_repair.actionable.append(c)

    # 6) progress: a per-roadmap STATUS.md / PROGRESS.md report is due in TauCetiRoadmap. Unlike every
    #    other kind this is not about a PR of ours, so it carries a single pr=0 candidate whose reason
    #    is the cadence verdict. Deep only: the check costs an API call, and the shallow survey exists
    #    to be cheap. progress_due never raises.
    if deep and "progress" in enabled:
        due, reason = progress_due(cfg, counters)
        c = Candidate(0, "", reason or "progress report")
        (sv.progress.actionable if due else sv.progress.suppressed).append(c)

    if deep and enabled & {"overlap-scan", "overlap-fix"}:
        from .overlaps import survey_overlaps

        survey_overlaps(cfg, gh, sv)
    sv.discovery_gaps = len(gaps)
    sv.next_auto_stage = _next_auto_stage(sv)
    if deep:
        from .github_transport import event

        event(
            {
                "kind": "survey_result",
                "source": "compact" if available else "direct",
                "roadmap": sv.roadmap_only,
                "prs": len(prs),
                "inventory_prs": len(raw),
                "elapsed_ms": round((time.monotonic() - began) * 1000),
                "issue_gaps": sum(not row["review"]["issue_complete"] for row in compact or []),
                "review_gaps": sum(not row["review"]["review_complete"] for row in compact or []),
            }
        )
    return sv


def _next_auto_stage(sv: Survey) -> str | None:
    for stage in AUTO_STAGES:
        if sv.kind(stage).actionable:
            return stage
    if not sv.roadmap_backpressure and not sv.discovery_gaps:
        return "roadmap"
    return None

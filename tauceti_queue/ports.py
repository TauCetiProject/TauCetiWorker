"""The ports the queue controller depends on: GitHub as a protocol plus the records it exchanges.

The controller (`tauceti_queue.controller`) never talks to GitHub directly. It is handed an object
implementing `GitHubPort`; the production adapter (App authentication, webhooks, REST and GraphQL
calls) implements it, and `tauceti_queue.fakes.FakeGitHub` implements it over a local bare
repository for the end-to-end fixture. Every method takes the repository as `owner/repo`.

Timestamps are unix seconds (floats); `None` means unknown or not yet.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol


class GitHubUnavailable(RuntimeError):
    """GitHub could not answer (rate limit, 5xx, network error), as opposed to answering no.

    Raised by `repository_admin` and the adapter's `user_for_token` so callers can keep serving a
    cached answer instead of refusing or granting anything new during a quota outage.
    """


@dataclass(frozen=True)
class PullRequest:
    """A pull request as read from GitHub at one moment."""

    number: int
    head_sha: str
    base_ref: str
    title: str
    body: str
    author_login: str
    state: str  # "open" or "closed"
    draft: bool = False
    labels: tuple[str, ...] = ()


@dataclass(frozen=True)
class OpenPullRequest:
    """One open PR from the repository listing, with the review App's `merge eligibility` check runs on its head.

    `eligibility_checks` are check-run records in the REST shape `eligibility.latest` reads (`id`,
    `name`, `app.id`, `head_sha`, `status`, `conclusion`, `external_id`, `completed_at`, `html_url`),
    already restricted to the review App's checks of that name; empty when the head carries none.
    """

    number: int
    head_sha: str
    base_ref: str
    draft: bool = False
    eligibility_checks: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True)
class CandidatePhase:
    """One build phase of a candidate run, as the workflow publishes it (preparation, cache restore, build, audits, lint)."""

    name: str
    started_at: float | None
    completed_at: float | None
    status: str  # "queued", "in_progress", "completed", "skipped"
    detail: str = ""  # free text, for instance module counts


@dataclass(frozen=True)
class CandidateRun:
    """The workflow run that `dispatch_candidate` started for one (batch, head, base), with its placement and progress.

    `diagnostics` are the source findings the adapter extracted from the run's annotations, logs
    or finalizer summary once the run completed: dicts with at least `module`, `kind` (`compiler`,
    `audit`, `lint`, `infrastructure`) and `message`. The controller hands them to the repair task
    unchanged and uses `kind == "infrastructure"` findings only for classification.
    """

    run_id: str
    attempt: int
    status: str  # "queued", "in_progress", "completed"
    conclusion: str | None  # "success", "failure", "cancelled", "timed_out", "skipped", "stale", ...
    created_at: float | None
    started_at: float | None
    completed_at: float | None
    runner_name: str | None
    runner_labels: tuple[str, ...]
    host: str | None
    phases: tuple[CandidatePhase, ...] = ()
    diagnostics: tuple[dict[str, Any], ...] = ()
    display_title: str = ""
    html_url: str = ""


@dataclass(frozen=True)
class Status:
    """A commit status (one context on one SHA). `run_id` is the run whose finalizer posted it, when known."""

    state: str  # "pending", "success", "failure", "error"
    description: str
    target_url: str
    updated_at: float | None
    run_id: str | None


@dataclass(frozen=True)
class FastForwardResult:
    """Outcome of a fast-forward of a branch from `expected_parent` to a tip."""

    ok: bool
    actual_parent: str | None  # the branch tip GitHub reported when the update was refused or when known
    error: str = ""


@dataclass(frozen=True)
class GitHubRateLimit:
    remaining: int
    reset_at: float


class GitHubPort(Protocol):
    """Everything the controller needs from GitHub.

    Implementations must be safe to call from the controller loop thread and the HTTP server
    thread concurrently. `post_comment` and `set_labels` are idempotent: a comment carrying `marker`
    is updated rather than duplicated, and labels already in the requested state are left alone.
    """

    def fetch_pr(self, repo: str, number: int) -> PullRequest: ...

    def fetch_refs(self, repo: str, refspecs: Sequence[str]) -> None:
        """Bring the named objects into the service's local clone (`git fetch origin <refspec>...`)."""
        ...

    def dispatch_candidate(
        self, repo: str, *, head_sha: str, base_sha: str, base_ref: str, batch_id: str, members: Sequence[int]
    ) -> str:
        """`repository_dispatch` of `pr-build.yml` with event `tauceti-bors-staging`; returns a dispatch id."""
        ...

    def find_candidate_run(
        self, repo: str, *, batch_id: str, head_sha: str, base_sha: str, since: float | None = None
    ) -> CandidateRun | None:
        """The run whose display title is `bors branch=<base_ref> batch=<batch_id> head=<head_sha> base=<base_sha>`.

        `since` is the dispatch time of the attempt being observed: an infrastructure re-dispatch
        of the same head, base and batch produces a second run with the same title, and the newest
        run created at or after `since` (less a minute of skew) is the one that answers for it.
        """
        ...

    def commit_status(self, repo: str, sha: str, context: str) -> Status | None: ...

    def fast_forward(self, repo: str, branch: str, expected_parent: str, tip: str) -> FastForwardResult: ...

    def update_ref(self, repo: str, ref: str, sha: str) -> None:
        """Create or move a ref (used for the retained `refs/tauceti-queue/...` namespace)."""
        ...

    def update_refs(self, repo: str, updates: Sequence[tuple[str, str]]) -> None:
        """Create or move several refs in one push (`(ref, sha)` pairs); the controller falls back to
        `update_ref` for each when the batched push fails, or when a port lacks this method."""
        ...

    def post_comment(self, repo: str, number: int, body: str, marker: str) -> str:
        """Post (or update) the comment carrying `marker`; returns the comment id."""
        ...

    def set_labels(self, repo: str, number: int, add: Sequence[str], remove: Sequence[str]) -> None: ...

    def close_pr(self, repo: str, number: int, title: str) -> None:
        """`PATCH pulls/<number> {state: closed, title}`: how Bors closes a squash-landed member (not GitHub's merged state)."""
        ...

    def repository_admin(self, login: str) -> bool:
        """Whether `login` has admin permission on the repository (the `security_audit.require_admin` predicate).

        Raises `GitHubUnavailable` when GitHub could not answer; the controller then serves its cached answer.
        """
        ...

    def rate_limit(self) -> tuple[int, float]:
        """(remaining calls, unix time of the reset)."""
        ...

    def eligibility_checks(self, repo: str, head: str) -> list[dict[str, Any]]:
        """Every `merge eligibility` check run on `head`, any App, as Bors lists them (`filter=all`)."""
        ...

    def pr_merge_base(self, repo: str, number: int, head_sha: str) -> str | None:
        """The merge base of the PR's head and its base branch, or None when the head is no longer live."""
        ...

    def list_open_pull_requests(self, repo: str, *, review_app_id: int) -> list[OpenPullRequest]:
        """Every open PR with the review App's `merge eligibility` check runs on its head (the approval poll's listing)."""
        ...


# --- records exchanged with workers over the task API ---------------------------------------------


@dataclass
class TaskMember:
    """A member as the repair task sees it: identity, accepted source, current commit and provenance."""

    source_pr: str
    number: int
    accepted_head: str
    commit: str | None
    provenance: str  # "clean" or "edited"
    title: str = ""
    description: str = ""
    author: str = ""
    explanation: str = ""
    retained_ref: str = ""


@dataclass
class Task:
    """A claimed repair or rebase-conflict task.

    `base` and `tip` are the chain the agent starts from. For a `rebase_conflict` task `onto` is
    the canonical commit the replacement chain must be based on and `tip` the chain that failed to
    replay; for a `repair` task `onto` equals `base`. `diagnostics` are the CI findings (repair) or
    the failed replay's conflict evidence (rebase conflict). `retained_refs` name the refs under
    which the inputs and the current chain can be fetched.
    """

    task_id: int
    kind: str  # "repair" or "rebase_conflict"
    repository: str
    batch_id: int
    batch_version: int
    base: str
    tip: str
    onto: str
    members: list[TaskMember]
    diagnostics: list[dict[str, Any]]
    retained_refs: list[str]
    submission_ref: str
    claim_token: str
    expires_at: float
    instruction: str
    created_at: float
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "kind": self.kind,
            "repository": self.repository,
            "batch_id": self.batch_id,
            "batch_version": self.batch_version,
            "base": self.base,
            "tip": self.tip,
            "onto": self.onto,
            "members": [vars(member) for member in self.members],
            "diagnostics": self.diagnostics,
            "retained_refs": self.retained_refs,
            "submission_ref": self.submission_ref,
            "claim_token": self.claim_token,
            "expires_at": self.expires_at,
            "instruction": self.instruction,
            "created_at": self.created_at,
            "extra": self.extra,
        }


@dataclass
class Adoption:
    """The controller's answer to a submitted chain.

    `accepted` means the chain is now the batch's authoritative tip at `version`. A rejected
    submission keeps its commits retained under `retained_ref` so they can be reused; `reason` is a
    stable code (`claim-expired`, `claim-superseded`, `version-mismatch`, `not-authorized`,
    `invalid-chain`, `explanation-required`, `unknown-task`) and `violations` carries the chain
    tool's findings when there are any.
    """

    accepted: bool
    reason: str
    task_id: int
    batch_id: int | None
    version: int | None
    tip: str | None
    retained_ref: str | None
    violations: list[dict[str, Any]] = field(default_factory=list)
    provenance: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return vars(self) | {"violations": list(self.violations), "provenance": dict(self.provenance)}


AGENT_INSTRUCTION = (
    "Resolve the reported conflicts or CI failures in this batch. Preserve the constituent PRs' intent as best "
    "you can; where they conflict, use your judgment to choose the best alternative or combination. Update the "
    "constituent commits, retaining their source PR identities. When you change a commit's content, append a very "
    "concise explanation of what changed and why to its existing message. If a contribution cannot be made to "
    "pass, reduce it to the part that does, down to an empty commit, and say so in its message."
)

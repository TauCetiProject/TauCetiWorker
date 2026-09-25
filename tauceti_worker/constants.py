"""tauceti_worker.constants — repo names, per-PR budgets, loop timing, rate-limit regexes, and the
agent/task tables."""

from __future__ import annotations

import os
import re

TAUCETI = "TauCetiProject/TauCeti"

TAUCETI_OWNER = TAUCETI.split("/", 1)[0]  # base-repo owner: a bot PR is first-party iff its head lives here

ROADMAP = "TauCetiProject/TauCetiRoadmap"

REVIEW = "TauCetiProject/TauCetiReview"

# The shared cooperative-claim namespace: a repository that holds nothing but `refs/tauceti-claims/*`
# leases, so operators can coordinate without anyone holding write access to canonical. Push access is
# granted automatically to the author of any merged TauCeti PR (canonical's `claims-access` workflow);
# until then a worker claims in its own fork instead. See github.claims_repo.
CLAIMS = "TauCetiProject/tauceti-claims"


# Per-PR budgets (a PR can never churn forever).
MAX_FIX_ATTEMPTS = 3  # per-head: stop re-running the fixer on a commit it can't change (a stuck

# head never advances a review round, so CI's round cap can't catch it).
# The review-ROUND budget lives in CI now (TauCeti housekeeping closes a PR reviewed to its cap while
# still blocking). The worker no longer caps its own review rounds — it keeps reviewing on every new
# head until the PR merges or CI closes it — so every PR reaches a terminal state.
MAX_INFRA_REFUNDS = 20  # per-head: how many times a provider outage may hand an attempt back before
# the budget starts charging anyway. Not a cost control — the escalating loop back-off already caps
# retries at ~4/hour — but a stop on MISCLASSIFICATION: if some persistent, PR-specific failure ever
# matched the transient patterns, an uncapped refund would retry it until a human noticed.
MAX_REVIEW_ERRORS = 3  # per PR: after this many review rounds that ERROR without posting a verdict

# The tauceti-review engine's exit status for "I stopped because the provider is unusable, and I
# posted nothing" (TauCetiReview `runner/review.py: PROVIDER_DOWN_EXIT`). It is a separate status
# precisely so this side can tell an outage from a review that failed on its own merits, and decline
# to charge a PR for it. Kept distinct from the numbers the engine returns for ordinary failure; if
# the engine ever renumbers, do_review would simply stop recognising the carve-out and go back to
# charging, which is the pre-existing behaviour rather than a new failure mode.
REVIEW_PROVIDER_DOWN_EXIT = 3

# Progress reporting (TauCetiProgress). Pinned by SHA, not a branch: the worker's generator and the
# merge gate in TauCetiRoadmap must run the SAME version, or the worker can emit headers the gate does
# not recognise and every report wedges. Bump this together with the two pins in
# TauCetiRoadmap/.github/workflows/progress-*.yml.
PROGRESS = os.environ.get("TAUCETI_PROGRESS_REPO", "TauCetiProject/TauCetiProgress")
PROGRESS_REF = os.environ.get("TAUCETI_PROGRESS_REF", "dbb229dec0434fd7faaa18a79c776b18f18a8252")
PROGRESS_TTL = int(os.environ.get("TAUCETI_PROGRESS_TTL", "600"))  # seconds a `due` verdict stays fresh
MAX_PROGRESS_ERRORS = 3  # consecutive failed progress rounds before backing off
PROGRESS_ATTEMPT_GAP = int(os.environ.get("TAUCETI_PROGRESS_GAP", "28800"))  # min seconds between attempts
# Lines of a failing `tauceti-progress` subcommand echoed into the main log. The whole output is
# saved to a file regardless; this is only how much of it a reader sees without opening that file.
# Matches the 20 lines `agents.run_to_logfile` tails for the review engine.
PROGRESS_TOOL_TAIL = 20
# And a cap on each of those lines, because a line COUNT bounds nothing: a tool that dies without
# printing a newline emits one line, and that line was reaching both the main log and the exception
# message at its full length. Wide enough for a traceback's last line, which is the one that matters.
PROGRESS_TOOL_LINE = 500

# (the engine can't produce a review at all), stop retrying and ESCALATE —
# a loud per-round warning + a tracking issue — since a PR that can never be
# reviewed neither merges nor reaches CI's round cap, so a human must step in.
MAX_REVIEW_CONTESTS = 10  # per-PR lifetime cap on author-contest re-reviews (no-bar: anyone may

# reply, so this bounds spend; separate from the review-round budget)
MAX_REVIEW_CONTESTS_PER_RUBRIC = 3  # per-rubric cap so one noisy thread can't starve the PR's budget

# The review ENGINE enforces a per-PR daily round cap (its --max-rounds-per-day, default 12); once hit it
# refuses to review but still exits 0 after re-posting the scoreboard. The worker mirrors that number so it
# can SKIP a capped PR during the survey — before launching the engine (and its expensive clones) — instead
# of re-selecting it every round and tight-looping. MUST stay in sync with the engine default (review.py).
REVIEW_DAILY_CAP = int(os.environ.get("TAUCETI_REVIEW_DAILY_CAP", "12"))

# Soft reviewer affinity: the publisher of the latest scoreboard gets first refusal on a new
# reviewable head (or an author contest) for twenty minutes. After that the unit returns to the shared
# fleet queue. Fixed fleet policy rather than an operator knob: reviewers must agree on the same grace.
REVIEW_AFFINITY_GRACE_S = 20 * 60

# Review candidates are drawn by an age-weighted lottery. One hour of waiting adds one unit of weight;
# cap at one day so old work is strongly preferred without making every worker converge deterministically
# on the single oldest PR.
REVIEW_AGE_SCALE_S = 60 * 60
REVIEW_AGE_CAP_S = 24 * 60 * 60

CONTEST_CLAIM_TTL = 3600  # seconds a 👀 on the contested reply claims an in-flight contest re-review.

# The claim lives on GitHub (a reaction on the reply comment), so it dedups
# ACROSS the fleet — unlike a per-worker counter, which can't coordinate
# between isolated stores. Removed once the round publishes; this TTL is only
# the backstop that frees a claim left by a worker that crashed mid-review.
CONTEST_CLAIM_EMOJI = "eyes"

MAX_CI_ATTEMPTS = 3  # per-head: stop trying to green a red-CI head

MAX_CI_PR_ATTEMPTS = 5  # per-PR lifetime backstop for red-CI fixing

MAX_REBASE_ATTEMPTS = 3  # per-PR: stop trying to rebase a conflicting PR

MAX_BUMP_ATTEMPTS = 3  # per-head: stop trying to green a red bump-mathlib head

MAX_BUMP_PR_ATTEMPTS = 5  # per-PR lifetime backstop for bump fixing across heads

BUMP_HEAD_PREFIX = "bump-mathlib/"  # branch prefix the review bot opens its mathlib-bump PRs on
MAX_LINT_REPAIR_ATTEMPTS = 3  # per-head: stop trying to green a red lint-repair head
MAX_LINT_REPAIR_PR_ATTEMPTS = 6  # per-PR lifetime backstop for lint repair across heads
# Branch prefix of the repair PR TauCeti's daily full lint (.github/workflows/lint-full.yml) opens
# when main carries environment-lint violations that PR builds, which lint only changed modules,
# could not see.
LINT_REPAIR_HEAD_PREFIX = "lint-repair/"

# Backpressure: don't author into the selected roadmap scope while this many of our PRs in that scope
# are open.
MAX_OPEN_PRS = 8

# The status labels TauCeti's CI keeps on every open PR to track where it sits in the review pipeline.
# The survey counts open PRs into these buckets for the per-round "open PRs" line, in lifecycle order
# (a PR climbs CI -> review -> author fixes -> merge). `ci-failed` and `awaiting-author` are both
# author-action states, split because a red build is read in the build log and a changes request in
# the review threads. Fixed set: a new status label won't appear here
# until it is added, which keeps the line stable and its columns comparable round to round. These are
# not a partition — a PR carrying none of them lands in no bucket, and the roadmap/* area labels are a
# separate axis that this line ignores.
STATUS_LABELS = (
    "awaiting-CI",
    "awaiting-review",
    "review-in-progress",
    "ci-failed",
    "awaiting-author",
    "ready-to-merge",
)


# Loop timing. Env-overridable for tuning and tests.
POLL = int(os.environ.get("TAUCETI_POLL", "300"))  # seconds between quota checks while waiting

ROUND_TIMEOUT = int(os.environ.get("TAUCETI_ROUND_TIMEOUT", "5400"))  # 90 min hard cap per round

INTERROUND = int(os.environ.get("TAUCETI_INTERROUND", "20"))  # min gap after a PRODUCTIVE round

EX_NOPROGRESS = 75  # round did NO productive work (distinct from error=1 / success=0)

BACKOFF_BASE = int(os.environ.get("TAUCETI_BACKOFF_BASE", "30"))  # first no-progress sleep (doubles each round)

BACKOFF_MAX = int(os.environ.get("TAUCETI_BACKOFF_MAX", "900"))  # cap on the escalating sleep (15 min)

# The escalating back-off exists because a no-op round must NOT re-cycle every INTERROUND seconds and
# re-hammer the API — the failure that ran ~700 no-op rounds against a rate-limited GitHub.

# GitHub REST rate-limit handling. We pace LLM quota carefully; GitHub's REST budget needs the same
# care, or a 403 mid-round throws away the agent's (expensive) work. Two layers:
#  - gh_run() waits IN PLACE for a rate limit to clear and retries (so a transient limit costs a pause,
#    not a lost round), bounded by GH_INROUND_WAIT so it can't blow ROUND_TIMEOUT.
#  - cmd_loop preflights core budget BEFORE launching a round (no hard cap there) — the right place to
#    wait out an hourly primary reset, and what keeps us from launching the review engine (whose own
#    diff fetch would 403) without enough budget to finish.
GH_MIN_BUDGET = int(os.environ.get("TAUCETI_GH_MIN_BUDGET", "200"))  # core requests a round needs to finish

GH_INROUND_WAIT = int(os.environ.get("TAUCETI_GH_INROUND_WAIT", "900"))  # cap on gh_run's in-place wait (15 min)

GH_SECONDARY_BASE = 60  # first secondary-limit sleep when no Retry-After is given (then exponential)

# Transient GitHub failures: a 5xx from the API gateway, or a response that died mid-body. Distinct
# from a rate limit — nothing is throttling us, the request simply did not survive. The survey's
# opening `gh pr list` asks for statusCheckRollup over every open PR, which takes ~10s server-side and
# is answered with an HTTP 504 often enough to abort whole rounds. A retry a few seconds later almost
# always lands, so these get a short in-place retry (see gh_run) rather than costing a round.
# The open-PR survey that opens every round. It used to be one `gh pr list --limit 200` carrying
# statusCheckRollup, whose cost grew with the number of open PRs: at ~100 PRs it took ~10s server-side
# against a GraphQL gateway that gives up around 11, and past 200 PRs it would have started silently
# dropping the rest. The survey pages instead, so the cost of any ONE request is fixed by the page size
# no matter how large the project grows, and no page is anywhere near the gateway's patience.
OPEN_PR_PAGE = int(os.environ.get("TAUCETI_OPEN_PR_PAGE", "100"))  # PRs per request (GitHub's maximum)

OPEN_PR_MAX_PAGES = int(os.environ.get("TAUCETI_OPEN_PR_MAX_PAGES", "100"))  # refuse to loop forever

GH_TRANSIENT_TRIES = 3  # retries after a transient failure, then surface it

GH_TRANSIENT_BASE = 5  # first transient-failure sleep, doubling per retry (5s, 10s, 20s)

_GH_PRIMARY_RE = re.compile(r"(?:API )?rate limit exceeded|rate limit.*exceeded", re.I)

_GH_SECONDARY_RE = re.compile(r"secondary rate limit|abuse detection", re.I)

# Each of these is the transport or the server failing, never a verdict about our request: a 5xx, a
# body that stopped arriving (gh reports the truncation as a JSON parse error), or a dropped
# connection. Deliberately narrow — a 4xx is an answer, and retrying one just repeats it.
# A GraphQL document that opens with `mutation` writes; anything else (a `query`, or the bare `{...}`
# shorthand) reads. Leading comments are skipped so a commented document is still classified by its
# operation.
_GQL_MUTATION_RE = re.compile(r"\A\s*(?:#[^\n]*\n\s*)*mutation\b")

_GH_TRANSIENT_RE = re.compile(
    r"HTTP 5\d\d|unexpected end of JSON input|connection reset by peer|i/o timeout|TLS handshake timeout",
    re.I,
)


# Claims / scoreboard cache.
CLAIM_TTL_S = int(os.environ.get("CLAIM_TTL", "1500"))  # 25 min lease; expires if a worker stops heartbeating

CLAIM_HEARTBEAT_S = int(os.environ.get("CLAIM_HEARTBEAT", "300"))  # renew every 5 min while the agent runs

SBCACHE_TTL = int(os.environ.get("TAUCETI_META_TTL", "120"))  # seconds a cached scoreboard meta stays fresh

# How long a cached comment read may be served on the strength of the PR's `updatedAt` alone (see
# ReviewState.observe). Nothing about a PR's comments can change without GitHub bumping that clock —
# measured over 179 in-place scoreboard edits, none landed newer than their PR's `updatedAt` — with the
# one exception it cannot express: a DELETED comment leaves no timestamp behind. This backstop is the
# bound on that blind spot, so a deleted or forged scoreboard is refetched within half an hour even
# though nothing announced it. Deliberately not "until the next reset": a heuristic we cannot verify
# gets a ceiling. A read served under this rule is `assumed`, never `fresh`, and cannot authorize a
# mutation; see dispatch()'s revalidation of the one PR a round acts on.
SBCACHE_BACKSTOP_S = int(os.environ.get("TAUCETI_META_BACKSTOP", "1800"))

COMMENTS_MEMO_S = 5  # in-memory window over which one survey pass coalesces its issue-comment fetches

# (scoreboard meta + in-flight marker share one read); << the round/dashboard cadence

# In-flight review de-contention. The review engine (TauCetiReview) posts a PR comment marking a head
# as under review and embeds an `expires_at` so a crashed reviewer self-clears. De-contention is on the
# head ALONE (a commit is reviewed once, regardless of model), and the engine's coordinate() remains the
# authoritative claim. The worker reads the SAME marker during the survey so it can skip a head a peer is
# already reviewing BEFORE paying the engine's build+launch cost — and, crucially, without busy-looping
# on the one PR a peer holds. The marker format is owned by the engine; we parse only the head and the
# expiry, so the engine's TTL value stays its own concern.
REVIEW_INPROGRESS_RE = re.compile(r"<!--tauceti-review-in-progress (.*?)-->", re.S)


# Agents.
OPENROUTER_MODELS = {
    "deepseek": os.environ.get("DEEPSEEK_MODEL", "deepseek/deepseek-v4-pro"),
    "minimax": os.environ.get("MINIMAX_MODEL", "minimax/minimax-m3"),
}

AGENT_NAMES = {
    "codex": "Codex",
    "claude": "Claude Code",
    "kiro": "Kiro",
    "deepseek": "DeepSeek",
    "minimax": "MiniMax",
}

# Reproducible authoring defaults. Provider selection remains quota-driven; once
# selected, host and bubble launchers consume this exact model/effort profile.
# Review models are configured separately by the review engine.
CODEX_AUTHORING_FALLBACK_MODEL = "gpt-6-luna"
# A model entitlement normally changes only when an account's subscription changes. Keep the
# side-effect-free access probe out of every round while still noticing an upgrade promptly.
CODEX_MODEL_ACCESS_TTL = 3600
AUTHORING_DEFAULTS = {
    # Prefer flagship Sol for authoring. A cached preflight probe selects Luna only when Codex confirms
    # that this repository default is unavailable to the current subscription.
    # Pin Claude to the current exact Opus generation, not its moving alias.
    "codex": ("gpt-6-sol", "high"),
    "claude": ("claude-opus-5-5", "high"),
    # Never use Kiro's Auto router. Operators can select another exact entitled
    # id (for example claude-opus-5) with the existing --author-model flag.
    # Kiro does not serve gpt-6-sol yet (https://kiro.dev/changelog/models/).
    "kiro": ("gpt-5.6-sol", "high"),
}

PI_RUN = os.environ.get("PI_RUN", os.path.expanduser("~/.claude/skills/pi/scripts/run.sh"))

# $TAUCETI_CLAUDE_CMD overrides the `claude` executable for host rounds (a sandbox wrapper, a
# differently-named build, ...); it's split as a shell word list and the standard
# -p/--model/--permission flags are still appended. Matches PI_RUN / $TAUCETI_BUBBLE /
# $TAUCETI_CODEX_MODEL. (Bubble rounds run claude inside the container, so this is host-mode only.)
CLAUDE_CMD = os.environ.get("TAUCETI_CLAUDE_CMD", "claude")


# Task taxonomy. Every task drives a model; merge/abandon/dedup housekeeping lives in the repo's CI now.
# `progress` writes the per-roadmap STATUS.md / PROGRESS.md reports in TauCetiRoadmap.
ALLOWED_TASKS = ["rebase", "review", "fix-ci", "fix", "bump", "progress", "roadmap", "lint-repair"]

WORK_TASKS = list(ALLOWED_TASKS)

# Priority for an unrestricted round. Resolve conflicts and adapt a broken Mathlib bump first, then
# honor the project's globally paced progress reporting. The worker's fix/CI maintenance remains
# ahead of fleet-wide reviews so author-action work cannot be starved by unrelated reviews. Roadmap
# is the final fallback and is handled separately after these stages. The durable attempt breaker
# keeps a stuck or rejected progress report from burning every round.
AUTO_STAGES = ("rebase", "bump", "lint-repair", "progress", "fix-ci", "fix", "review")

# The work units that act on an EXISTING pull request, and so are the ones `--pr` can target. The two
# left out cannot be named by a PR number at all: `progress` writes a roadmap's generated reports
# rather than touching a PR of ours, and `roadmap` opens a PR that does not exist yet. Both carry a
# pr=0 candidate, which is why no `--pr` value is allowed to be 0.
PR_TASKS = ("rebase", "bump", "lint-repair", "fix-ci", "fix", "review")

# The "#" shown in the survey table IS the key you press in the TUI to run one round of that kind.
# ALLOWED_TASKS deliberately stays the stable display/key order; AUTO_STAGES is the unrestricted
# runtime priority. Keeping those concepts separate avoids silently rebinding established digit keys.
KIND_KEYS = {str(i): name for i, name in enumerate(ALLOWED_TASKS, 1)}  # "1" -> "rebase", ...

KIND_BY_NAME = {name: num for num, name in KIND_KEYS.items()}  # "rebase" -> "1", ...

# Every mode runs a MODEL on third-party content, so each is eligible for the bubble sandbox
# (opt in with --bubble; the host is the default).
SANDBOX_DEFAULT = {t: True for t in WORK_TASKS}
# `progress` is the exception: there is no untrusted checkout to confine. It needs `gh` against
# TauCetiRoadmap (the bubble proxy is scoped to TauCeti) and the model is handed bounded text rather
# than a working tree to roam. Its remaining exposure — merged PR descriptions reaching the model — is
# bounded by the merge gate, which only ever admits two markdown files in one directory.
SANDBOX_DEFAULT["progress"] = False


AGENTS = ["auto", "codex", "claude", "kiro", "deepseek", "minimax"]

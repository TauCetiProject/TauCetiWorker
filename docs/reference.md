# `tauceti work` reference

`tauceti work` does one round and exits; `--loop` runs the driver. The same flag
list is in `tauceti work -h`. For persistent workers, see
[the workers documentation](workers.md).

## Flags

| Flag | What it does |
| --- | --- |
| `--loop` | Run the driver: keep doing rounds, pacing against quota between them, instead of one. |
| `--only TASKS` | Restrict the round to a comma list of `rebase,bump,progress,fix-ci,fix,review,roadmap` (default: the whole cascade). |
| `--skip TASKS` | Drop a comma list of tasks from the cascade. Combines with `--only` by subtraction. |
| `--agent AGENT` | `auto` (default), `codex`, `claude`, `kiro`, `deepseek`, or `minimax`. Kiro and OpenRouter providers are explicit-only and unpaced. |
| `--author-model MODEL` | Exact authoring model for an explicit provider (CLI > provider environment > committed default). |
| `--author-effort EFFORT` | Authoring reasoning effort for an explicit Codex, Claude, or Kiro provider. |
| `--account EMAIL_OR_ID` | Require the Codex credential to be this account (email, or the workspace UUID `tauceti doctor` prints) and refuse to run otherwise. Checks only; never switches. Needs an explicit `--agent codex`. |
| `--bubble` | Run code and review agents inside the Bubble sandbox instead of directly on the host. The outer survey and coordination, plus all progress-report rounds, remain on the host. |
| `--host` | Deprecated no-op: the host is now the default. It only warns; pass `--bubble` for the sandbox. |
| `--stream` | Stream the agent's log to the terminal instead of a file under `logs/`. |
| `--roadmap-only AREA` | The single roadmap area for roadmap rounds (empty = all areas). |
| `--roadmap-skip AREA[,AREA...]` | Roadmap areas to exclude from selection (`--roadmap-only` wins on overlap). |
| `--source PATH_OR_URL` | Supplementary local Git repository directory or Git repository URL (checked-out/default `HEAD`) for authoring a PR. A shallow snapshot is stored in worker state, refreshed on later rounds, and mounted read-only in Bubble mode. Requires the roadmap phase to be enabled and one specific `--roadmap-only AREA`; other enabled phases ignore it, and the roadmap and review quality remain authoritative. |
| `--roadmap-extra-identities LOGIN[,LOGIN...]` | Extra GitHub logins, beyond your `gh auth` identity, whose claimed intentions the worker treats as its own (won't avoid). |
| `--ignore-claims` | Don't avoid targets others have claimed on the intentions board (claim-respect is on by default). |
| `--auto-refresh` | Renew this worker's Claude access token when it expires, instead of reporting Claude unavailable until a human runs `claude` again. Off by default, and only safe when nothing else uses the same credential file — the refresh token is single-use, so the rotation logs out an interactive `claude`, a second refresher, or a copy of the credential elsewhere. See [quota and pacing](quota.md). |
| `--ignore-quota` | Ignore soft pacing for an explicit `--agent codex\|claude`; unreadable usage and provider hard limits still stop the round. Kiro and OpenRouter agents do not use the subscription pacer. |
| `--quota-cmd CMD` | External pacer, run as `<cmd> <agent>`: first stdout token = model to run, empty output or nonzero exit = wait. |
| `--pace T:B[,T:B...]` | Pacing curve as `time%:budget%` points (e.g. `0:10,50:70,90:90`): usage must remain below the interpolated budget; time 0/100 default to 0/100. Default is `60:40`; `0:0,100:100` gives the plain `used% < elapsed%` rule. |
| `--worker-id ID` | Run an independent worker under this name; any id but `default` also isolates its credential directories (`$HOME` on Linux; provider-specific Claude, Codex, and Kiro directories on macOS). |
| `--isolate-home` | Force that per-worker isolation even for the `default` id (a distinct id already implies it). |
| `--dry-run` | Survey and print the picker's decision; act on nothing. |

## Roadmap backpressure

The open-PR backpressure limit follows the roadmap scope you select. A pinned
area counts only your open PRs identified for that area; an all-areas or
automatic run counts roadmap PRs in every non-skipped area. Drafts, non-roadmap
PRs, and PRs for roadmaps outside the selected scope do not consume its authoring
limit. An open roadmap PR whose area is temporarily unknown counts conservatively
in every scope until its area label resolves.

## The claim namespace

Two workers must not spend two subscriptions writing the same report or fixing
the same PR. They avoid it by taking a lease before they start: a custom git ref
`refs/tauceti-claims/<key>` in some repository both of them can push to, acquired
with an atomic compare-and-swap (see `scripts/claim.sh`). Which repository that
is decides how far de-duplication reaches, and the worker picks it like this:

1. `$CLAIM_REPO`, verbatim, if you set it.
2. `TauCetiProject/tauceti-claims`, the shared namespace, if your account can
   push there. Then you de-duplicate against every other operator.
3. Otherwise your own fork, which you can always push to. Then you de-duplicate
   across your own workers, and only those.

Push access to the shared namespace is granted automatically once you have had a
pull request merged into TauCeti, and the worker accepts the invitation itself
(that repository, and no other). Until then your fleet coordinates in your fork,
so nothing waits on anybody. The shared repository holds nothing but leases: no
code, no Actions, and no relationship to write access on the canonical repo,
which no worker ever needs.

If you run several workers on one host or in several containers, they get the
same answer and coordinate with no configuration. Set `CLAIM_REPO` when you want
to pin a namespace of your own anyway, for instance to keep two fleets under
different `gh` accounts from contending with each other.

Claims are cooperative and fail-open. Honouring one only avoids duplicate work;
the guarantee that two workers cannot clobber each other's branch is the
`--force-with-lease` CAS in `git-safe-push`, which does not depend on claims at
all. So a claim that cannot be acquired (a namespace this account cannot push to,
a GitHub outage) is logged and the round proceeds unclaimed.

## Claims on the intentions board

These are a different mechanism from the leases above: a coarse, human-visible
statement of intent rather than a per-task lock.


Within an area, roadmap workers respect finer-grained claims registered by other
contributors on the [intentions board](https://github.com/leanprover-community/intentions):
an open issue in the roadmap repo labelled `intention` + `roadmap/<area>` that
someone has claimed is treated as theirs, and the worker is told not to author
it. "You" is your own `gh auth` identity. If you run workers under several
accounts, or coordinate with someone whose intentions you are fulfilling, list
those logins with `--roadmap-extra-identities` so the worker does not avoid your
own side's claims. This is cooperative and fail-open;
`--ignore-claims` or `TAUCETI_RESPECT_CLAIMS=false` opts out.

An assigned intention with the maintainer-applied `administrative-hold` label is different: every
worker avoids its scope, even when authenticated as the assignee and even with `--ignore-claims`.
Reading these holds is fail-closed, so a GitHub error stops roadmap authoring. The hold remains tied
to the normal intentions lifecycle: when its dated claim expires, the bot clears the assignee and
the hold becomes inactive.

## Codex model selection

The committed Codex authoring profile defaults to `gpt-5.6-sol`. Before the real
authoring task, the worker makes a tiny read-only Sol access probe and caches the
result for one hour for that worker and ChatGPT account. It selects
`gpt-5.6-terra` only after two consecutive structured 400, 403, or 404 rejections
that identify a model-access problem. Rate limits, server errors, context errors,
malformed output, and ordinary failures pause the round without downgrading. Both
probes are read-only, and the real authoring prompt is always executed exactly
once.

An explicit `--author-model`, `TAUCETI_AUTHORING_CODEX_MODEL`, or legacy
`TAUCETI_CODEX_MODEL` is a pin: it bypasses both the probe and the fallback.

A generic authoring override is rejected with `--agent auto`, because the model
or effort may not apply to whichever provider quota selection picks.

## Kiro exact-model selection

Kiro is explicit-only. The committed authoring default is `gpt-5.6-sol` at high
effort; `--author-model claude-opus-5` selects Kiro's current exact Opus ID.
Before either a host or Bubble launch, TauCeti runs
`kiro-cli chat --list-models --format json` and requires the requested exact ID
to be present. That command sends no prompt. A missing entitlement pauses the
round instead of invoking Kiro Auto or silently downgrading.

Reviews use the independent `TAUCETI_REVIEW_KIRO_MODEL` pin, defaulting to the
same exact Sol ID. Use `KIRO_API_KEY` for headless authentication or
`kiro-cli login` for a persisted browser login.

## Credit usage

`tauceti usage [--provider kiro|openrouter] [--json]` is a prompt-free,
read-only telemetry command. `--provider` is repeatable and defaults to both.
`--kiro-burn-rate CREDITS` and `--openrouter-burn-rate USD` add estimated rounds
remaining to the report; they do not pace or select a provider. The equivalent
environment defaults are `TAUCETI_KIRO_BURN_RATE` and
`TAUCETI_OPENROUTER_BURN_RATE`.

Kiro usage comes from the CLI's ACP extension and retains fractional credit
values. OpenRouter's inference key reports key usage/limits; an optional
`OPENROUTER_MANAGEMENT_KEY` adds account-wide purchased-credit telemetry.

## Codex accounts

`--account EMAIL_OR_ID` (or `TAUCETI_ACCOUNT`) requires the Codex credential to
belong to a particular account, and exits the round before spending anything if
it does not. It checks; it never switches. This is Codex-only because its
credential carries the account identity, where `codex login status` prints only
"Logged in using ChatGPT". `tauceti doctor` shows which account the current
credential is for.

To change accounts outright, `codex logout && codex login`. Two things to know:
the browser flow has no account picker, so it completes as whichever ChatGPT
account your browser is already signed into, and `codex logout` revokes the old
session rather than merely forgetting it locally.

To run TauCeti on one account while your interactive `codex` keeps another, give
it a private credential directory instead of logging out:

```bash
CODEX_HOME=~/.codex-tauceti codex login
CODEX_HOME=~/.codex-tauceti tauceti work --agent codex --account you@example.com
```

## Environment variables

Flags win over these. Most are tuning knobs with sane defaults.

| Variable | Default | Effect |
| --- | --- | --- |
| `TAUCETI_AGENT` | `auto` | Default for `--agent`. |
| `TAUCETI_ACCOUNT` | _(unset)_ | Default for `--account`. |
| `CODEX_HOME` | `~/.codex` | Codex config/credential source. Point it at a private directory to give TauCeti its own Codex account without disturbing the one your interactive `codex` uses. |
| `TAUCETI_WORKER_ID` | _(unset)_ | Pin the id; when unset, `work` takes the lowest free `workerN`. |
| `TAUCETI_FORK` | auto-created | Point at an existing fork instead of the one the worker creates. |
| `TAUCETI_ROADMAP_ONLY` | _(unset)_ | The single roadmap area for `--roadmap-only`. Unset = a fresh random area each round (falls back to all areas if the list can't be fetched); `""` = all areas. |
| `TAUCETI_ROADMAP_SKIP` | _(unset)_ | Comma-separated roadmap areas to exclude, for `--roadmap-skip`. |
| `TAUCETI_ROADMAP_EXTRA_IDENTITIES` | _(unset)_ | Comma-separated extra GitHub logins whose claimed intentions count as the worker's own. |
| `TAUCETI_RESPECT_CLAIMS` | `true` | Whether roadmap workers avoid others' claimed intentions; `false` is the same as `--ignore-claims`. |
| `TAUCETI_QUOTA_CMD` | — | Default for `--quota-cmd`. |
| `TAUCETI_AUTO_REFRESH` | _(unset)_ | `1` is the same as `--auto-refresh`. |
| `TAUCETI_PACE` | _(unset)_ | Pacing curve for `--pace` (`time%:budget%` points); unset = `60:40`. |
| `TAUCETI_STREAM` | — | `1` is the same as `--stream`. |
| `CLAUDE_CONFIG_DIR` | `~/.claude` | Claude config/credential source (account switching; Bubble uses a private transient handoff on macOS). |
| `ELAN_HOME` | login user's `~/.elan` | Lean toolchains, shared by every worker: an install takes a lock and lands by rename. |
| `MATHLIB_CACHE_DIR` | `<worker state>/.cache/mathlib` | Where this worker downloads Mathlib artifacts. Private, because `lake exe cache get` takes no lock; finished files are exchanged with the machine pool by hardlink before each round. |
| `TAUCETI_MATHLIB_POOL` | `$XDG_CACHE_HOME/mathlib`, else login user's `~/.cache/mathlib` | The pool those hardlinks go to and come from. |
| `LAKE_CACHE_DIR` | `<worker state>/.cache/lake` | Lake's own build-output cache. Per-worker: unlike a toolchain install it is written throughout a build. |
| `TAUCETI_CLAUDE_CMD` | `claude` | The `claude` executable for host rounds; split as a shell word list, the usual flags appended. |
| `TAUCETI_INHERIT_CLAUDE_CONFIG` | _(unset)_ | `1` gives an isolated worker your own `CLAUDE.md`, `settings.json`, and skills instead of its own. Off by default: a round should not depend on whose config dir it ran from, and personal instructions can contradict the task prompt. |
| `TAUCETI_AUTHORING_CODEX_MODEL` / `TAUCETI_AUTHORING_CODEX_EFFORT` | `gpt-5.6-sol` (Terra fallback) / `high` | Codex authoring profile. An explicit model disables automatic fallback; unrelated host configuration remains available. |
| `TAUCETI_AUTHORING_CLAUDE_MODEL` / `TAUCETI_AUTHORING_CLAUDE_EFFORT` | `claude-opus-5` / `high` | Claude authoring profile; the default is an exact model rather than the moving `opus` alias. |
| `TAUCETI_AUTHORING_KIRO_MODEL` / `TAUCETI_AUTHORING_KIRO_EFFORT` | `gpt-5.6-sol` / `high` | Exact Kiro authoring profile. `claude-opus-5` selects Opus; Kiro Auto is never used. |
| `TAUCETI_REVIEW_CODEX_MODEL` | engine policy | Optional Codex review-model pin, independent of the authoring model. Unset preserves the review engine's own default and fallback. |
| `TAUCETI_REVIEW_KIRO_MODEL` | `gpt-5.6-sol` | Exact Kiro review-model pin, independent of authoring. |
| `TAUCETI_CODEX_MODEL` | _(deprecated)_ | Legacy fallback for the Codex authoring model only. Prefer `TAUCETI_AUTHORING_CODEX_MODEL`. |
| `DEEPSEEK_MODEL` / `MINIMAX_MODEL` | `deepseek/deepseek-v4-pro` / `minimax/minimax-m3` | OpenRouter model ids for those agents. |
| `OPENROUTER_API_KEY` | — | Required for `--agent deepseek\|minimax`; staged read-only into the bubble. |
| `OPENROUTER_MANAGEMENT_KEY` | — | Optional management key for account-wide `tauceti usage` credit totals; never passed to an agent. |
| `KIRO_API_KEY` | browser login | Optional headless Kiro credential. TauCeti isolates the browser store when set so the key wins deterministically. |
| `TAUCETI_KIRO_HOME` / `TAUCETI_KIRO_DATA_DIR` | per-worker when isolated | Internal redirects for Kiro settings and its platform-native browser-auth SQLite store. |
| `TAUCETI_KIRO_BURN_RATE` / `TAUCETI_OPENROUTER_BURN_RATE` | _(unset)_ | Observability-only default burn rates for `tauceti usage`; never used by the loop pacer. |
| `PI_RUN` | `~/.claude/skills/pi/scripts/run.sh` | The `pi` runner for OpenRouter agents on the host. |
| `TAUCETI_BUBBLE` | `bubble` (else `uvx` for dry-run probes only) | Override the Bubble executable. |
| `TAUCETI_BUBBLE_HOME` | per-worker cache dir | Override the private bubble home. |
| `TAUCETI_REVIEW_ENGINE_DIR` | — | Use a local `tauceti-review` checkout instead of fetching the engine. |
| `TAUCETI_POLL` | `300` | Seconds between quota checks while the loop waits. |
| `TAUCETI_ROUND_TIMEOUT` | `5400` | Hard cap per round (seconds). |
| `TAUCETI_INTERROUND` | `20` | Minimum gap after a productive round (seconds). |
| `TAUCETI_BACKOFF_BASE` / `TAUCETI_BACKOFF_MAX` | `30` / `900` | The escalating no-progress back-off (seconds). |
| `TAUCETI_PROGRESS_GAP` | `28800` | Minimum gap between progress-report attempts (seconds; eight hours by default). |
| `TAUCETI_GH_MIN_BUDGET` | `200` | GitHub requests (REST core and GraphQL) the loop requires before launching a round; below it on either bucket, the loop waits for the hourly reset. |
| `TAUCETI_GH_INROUND_WAIT` | `900` | Cap on how long a single `gh` call waits in place for a secondary rate limit to clear (seconds). Primary limits return immediately so the loop can wait for them before another round. |
| `TAUCETI_META_TTL` | `120` | How long a cached scoreboard stays fresh (seconds). |
| `CLAIM_REPO` | automatic | The repository holding this worker's cooperative claim leases. Without an override it is the shared namespace `TauCetiProject/tauceti-claims` once your account can push there, and your own fork until then. See [the claim namespace](#the-claim-namespace). |
| `CLAIM_TTL` / `CLAIM_HEARTBEAT` | `1500` / `300` | Branch-claim lease TTL and heartbeat interval (seconds). |

Worker configuration paths (`TAUCETI_WORKERS_CONFIG`, `TAUCETI_CONFIG_HOME`,
`TAUCETI_WORKERS_STATE_DIR`, `TAUCETI_RUNTIME_DIR`) are documented in
[the workers documentation](workers.md).

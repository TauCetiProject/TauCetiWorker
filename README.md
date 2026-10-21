Generic project configuration is provided by [TauCetiTools](https://github.com/TauCetiProject/TauCetiTools).
This repository remains an example worker harness. Pass the same configuration to work and review commands:

```sh
./tauceti --conf https://raw.githubusercontent.com/YOUR_ORG/YOUR_PROJECT/main/tauceti.toml doctor
./tauceti --conf ./tauceti.toml work --worker-id example
```

See the [architecture and manual setup](https://github.com/TauCetiProject/TauCetiTools/blob/main/docs/project-setup.md)
for repository ownership, Apps, credentials, workflow installation and optional services.
The manual also covers [roadmap authoring (WIP)](https://github.com/TauCetiProject/TauCetiTools/blob/main/docs/project-setup.md#roadmap-authoring-wip)
with TauCetiRoadmapWorker and a downstream-owned job board. This optional extra is disabled
by default; project `doctor` prints setup instructions and the dedicated roadmap doctor command.
A configured project has its own checkouts, review store, activity and snapshots under
`~/.local/state/tauceti/projects/OWNER__REPOSITORY/`. Starting a new worker reloads its canonical
configuration; a running process tree keeps its saved snapshot. Worker IDs in a shared manager
must remain unique. Commands without `--conf` retain Tau Ceti's existing behavior.

# Tau Ceti Worker

Cached round counts and authored/open/landed PR reporting are available with
`tauceti activity`; see [prepared activity reports](docs/activity.md). Status reads
prepared files only, and background reporting adds no GitHub API requests.

`tauceti` keeps the [TauCeti](https://github.com/TauCetiProject/TauCeti) Lean
library moving, using a "bring your own agent" approach. Run it with no command
and you get a dashboard of the work the queue needs right now: PRs to review,
fixes a review asked for, a Mathlib bump that needs adapting, roadmap targets.
From there you launch whatever you want. Pin a worker to one kind of work with
`--only` (a reviewer, a fixer, an author), or hand the whole thing to `--loop`
and let it pick the most useful job each round until you stop it.

It runs as your authenticated `gh` account: you set up `gh auth`, the worker acts
as that account, and it treats that account's own PRs as the ones it tends. The
repo is hardwired to `TauCetiProject/TauCeti`. This is an operator's tool for that
project, not a general framework. You author through **your own fork**: the worker
forks `TauCetiProject/TauCeti` once, automatically, pushes authored branches and
fixes there, and opens PRs from it, so you do **not** need write access to the
canonical repo. (A fine-grained token scoped only to the canonical repo is not
enough.) Set `TAUCETI_FORK=<owner>/<repo>` to use an existing fork instead.

Run as many workers as you like: they take a lease on each job so two of them
never write the same report or fix the same PR, and that needs no setup and no
write access either. Your fleet coordinates through your fork from the start,
and joins the shared namespace every operator uses once you have had a PR
merged. See [the claim namespace](docs/reference.md#the-claim-namespace).

## Quickstart

You need `gh`, `git`, `uv`, and `jq`. Log `gh` in as the account the worker should
act as, and log in to each subscription agent you want to use:

```bash
gh auth login
codex login            # for --agent codex, or auto
claude auth login      # for --agent claude, or auto
kiro-cli login         # for explicit --agent kiro
```

Code-writing phases on the host also need an `elan`/`lake` toolchain. The
OpenRouter agents need an exported `OPENROUTER_API_KEY` instead, plus the `pi`
runner for host rounds; Bubble includes `pi`. The `--bubble` sandbox has
additional requirements; see [the sandbox notes](docs/sandbox.md).

Install it as a tool, no clone needed:

```bash
uv tool install git+https://github.com/TauCetiProject/TauCetiWorker

tauceti doctor                     # report the tools and credentials this host can use
tauceti                            # the dashboard: see the available work, launch it
tauceti status                     # the same survey, non-interactive (--json for scripts)
tauceti verify --json              # synchronous local verification receipts, from a TauCeti checkout
tauceti usage --json               # prompt-free Kiro/OpenRouter credit telemetry
tauceti cost-model                 # agent, CI, cache, and marginal/cumulative LOC-cost estimates
tauceti work --only review         # one round of a specific kind of work, then exit
tauceti work --loop --only review  # a focused worker: keep reviewing (or fix / roadmap / ...)
tauceti work --loop                # fully automatic: keep picking the most useful job
```

Ctrl-C stops the current round and exits. From a clone you can run `./tauceti`
instead, a small PEP 723 `uv` shim that runs the same package; every command
below works either way, and this README writes the installed form.

Local checks use [concurrent verification with exact-input receipts](docs/verification.md).

### Multiple GitHub accounts

Log each account into `gh`, then select a login for each worker:

```bash
tauceti work --worker-id alice-review --github-account alice --only review --loop
tauceti work --worker-id bob-author --github-account bob --only roadmap --loop
tauceti --github-account alice                 # dashboard and its launches
tauceti doctor --github-account bob            # verify a selected account
tauceti workers add alice-review --github-account alice --only review
```

`--github-account` (or `TAUCETI_GITHUB_ACCOUNT`) selects the named stored `gh`
credential without changing your active `gh` login. A supplied `GH_TOKEN` or
`GITHUB_TOKEN` can serve as a fallback when there is no stored credential, but
must authenticate as the requested login. Loop children inherit and verify the
selected credential. GitHub API calls, HTTPS Git operations, commit attribution,
fork resolution, and PR maintenance all use that identity. Changing a persistent
worker's `github_account` setting restarts it. `--account` still checks the
Codex subscription account independently.

Accounts coordinate across their forks only when they use the same writable
claim repository: give each access to the shared namespace, or set `CLAIM_REPO`
to a repository they can all write. Bubble mode requires the companion
account-aware Bubble CLI and proxy; older installations reject the combination
before launching an agent. See [GitHub accounts](docs/reference.md#github-accounts).

## The dashboard

Bare `tauceti` opens an interactive dashboard ([Textual](https://textual.textualize.io/)).
The table lists each kind of work with a number, how many PRs are ready, and a
sample. The survey is fetched once in the background and refreshed on `r` (or
every 90s), so moving the cursor never re-queries GitHub. It reacts to single
keypresses, no Enter:

| Key | Action |
|-----|--------|
| `↑` / `↓` (or `k` / `j`) | move the cursor between kinds |
| `→` / `←` | expand / collapse the selected kind — list its PRs with titles (or, on `roadmap`, the areas) |
| `Enter` | run one round of the selected kind |
| `1`–`7` | run one round of that numbered kind directly |
| `l` / `L` | add a persistent worker for the auto cascade / selected kind |
| `o` / `x` | pick the single roadmap area (`--roadmap-only`) / edit the skipped areas (`--roadmap-skip`) |
| `m` / `s` | cycle the agent / toggle the sandbox (host ↔ bubble) |
| `w` | switch between available Work and desired/actual Workers |
| `r` / `c` / `q` | refresh / copy the launch command to the clipboard / quit |

In the Workers view, arrows select a worker, Space persists enabled/disabled
desired state, Ctrl-R restarts it, and Enter follows its current logfile.

Your agent, sandbox, and roadmap selections persist in `dashboard.json` under
the TauCeti config directory, so the dashboard reopens where you left it. They
are dashboard-only: a bare `tauceti work` never reads them, and an explicit
`TAUCETI_ROADMAP_ONLY` or `TAUCETI_ROADMAP_SKIP` in the environment still wins.
Clone-based and installed invocations share this user-level file. Over a pipe or
with no TTY the dashboard prints a one-shot snapshot instead. Use `tauceti
status` in scripts.

## What a round does

Mathlib adoption has two maintenance phases: `overlap-scan` examines batches
between a shared cursor and Tau Ceti's pinned Mathlib commit; `overlap-fix`
authors refactor PRs for labelled findings. They run after existing PR maintenance
and review, before new roadmap authoring. See [Mathlib overlaps](docs/mathlib-overlaps.md).

A round does exactly one unit of work: the first of these that applies.

| Step | What it does |
|------|--------------|
| **Rebase** | Reconcile one of our conflicting PRs or an authenticated current-head integration repair request, including missing sources or renamed APIs after main moves. Integrate one main snapshot and validate source repair before safe push. Genuine conflict retries renew after verified progress; handoffs retain their per-PR cap. Hold labels pause recovery. |
| **Bump** | Adapt a red `bump-mathlib/` PR (the review bot opens those to move the Mathlib dependency forward) so `TauCeti/` builds against the new Mathlib. The worker never opens a bump itself. |
| **Lint repair** | Fix `TauCeti/` on a red `lint-repair/` PR. PR builds lint only the modules a change touches, so TauCeti's daily full lint opens one of these when main carries environment-lint violations elsewhere (for example, a new simp lemma that takes an older one out of simp normal form). The worker never opens one itself. |
| **Progress** | When the global eight-hour cadence is due and a roadmap has at least 25 new PRs, update its generated `STATUS.md` and `PROGRESS.md` through TauCetiProgress. |
| **Fix CI** | Repair one of our PRs whose `build` check is red. It cannot be reviewed until it builds, so this comes before Fix. |
| **Fix** | Address the review findings on one of our PRs: fix the code, or contest a wrong finding on its thread. |
| **Review** | Review an open PR whose head is green but not yet reviewed, with the `tauceti-review` engine. Maintenance on our own PRs takes priority so author-action work (`ci-failed` or `awaiting-author`) cannot be starved by unrelated reviews. |
| **Overlap fix** | Refactor a labelled Mathlib overlap issue into a normal upstream PR, preserving additional Tau Ceti functionality. |
| **Overlap scan** | Examine up to 160 consecutive Mathlib commits after the shared cursor, publish actionable overlaps as issues, then advance the cursor through the completed batch. |
| **Roadmap** | Otherwise, open a new PR advancing a [roadmap](https://github.com/TauCetiProject/TauCetiRoadmap) target. |

Review selection is cooperative across community workers. The worker named by
the latest scoreboard gets a 20-minute first-refusal window on that PR's next
green head or contest reply. After that window, every worker may take it;
eligible reviews are chosen by an age-weighted lottery, with waiting-time weight
capped after 24 hours so older work is favored without imposing a rigid queue.

Merging green PRs, closing stuck ones, and de-duplicating are the repo's CI, not
the worker. A GitHub API failure aborts the round rather than reading as "nothing
to do", so a transient outage never falls through to authoring.

A review has two outputs with different roles. The head-pinned scoreboard posted
on the PR is the live verdict that auto-merge reads. Detailed run records are also
kept in a local outbox for the public TauCetiData analytics/provenance archive;
failure or lack of permission to publish that archive does not stop the posted
review from counting.

## Configure a round

Four independent dials: which work, which pull requests, which agent, and where
it runs. Combine them however you like.

### What work: `--only`

`security-audit` is an explicit, admin-only phase outside the normal cascade:

```bash
tauceti work --only security-audit --agent codex --dry-run
tauceti work --only security-audit --agent codex
tauceti work --only security-audit --agent claude --loop
```

An admin's maintenance worker can also use `--only security-audit,bump,progress`.
It inspects a bounded batch before ordinary maintenance when at least 20 commits
are pending. Below that threshold it continues with bump/progress work without
launching an audit model. Set `TAUCETI_SECURITY_AUDIT_MIN_COMMITS` to a positive
integer to change the threshold. Audit-only runs inspect any nonempty backlog.
Findings, unresolved inspections and operational failures stop audit advancement
and preserve their blocks and evidence. The combined worker continues with
bump/progress work; this never clears a block or bypasses release gating.
Other work phases and `--pr` cannot be
combined with the audit phase. Its isolated Luna/Haiku reviewer is unchanged.

It advances TauCeti's protected `security-audit` branch through main's first-parent
history, inspecting only `TauCeti/` and `TauCeti.lean` for IO and executable meta code.
Human-reviewed scripts, workflows, configuration and dependency pins are excluded.
Each session inspects complete per-commit diffs and before/after file contents,
up to 20 commits with a 2 MiB workload target. A single larger commit is inspected
alone without an aggregate byte cap, through the same compacting session. Each
source blob remains limited to 4 MiB, and each model launch to 30 minutes.
The batching target is separate from the model's live context window. Small inputs are supplied inline; larger
inputs are available through one read-only `audit.read_page` tool. It accepts only
a numbered page and a continuation token, with no filesystem paths, URLs, commands
or writes. Each read returns the token for the next call; a stale nonempty token
is rejected. To recover a lost token, explicitly send an empty token: the reader
replays the last full page with a fresh token. It does not count the requested new
page as read. The same Luna/Haiku conversation can compact its history and
re-read pages as needed, then returns one verdict covering the entire batch. There
are no independent partial inspections or combined partial approvals. A pass is
rejected unless the reader's receipt accounts for every page and the verdict echoes
the token that follows the last page's contents. Delivery coverage
does not prove the model understood the source; compaction can lose details, so
the prompt requires retaining findings and unresolved questions, re-reading when
needed, and returning incomplete if the full inspection cannot be completed.

Luna/Haiku receives no GitHub credential or general command/edit tools; trusted
Worker code alone verifies ancestry and moves the ref. In judgment protocol v3,
the model returns only `verdict` and `findings`, plus the final `read_token` for
paged inputs. The Worker attaches the immutable input digest and ordered commit
list from the launch record. Neither commit SHAs nor a digest are copied from
model output. Unknown response fields are rejected. Coverage and final-token
checks remain mandatory; host-owned identity does not establish source safety.
No Lean builds or candidate scripts run. A finding, incomplete inspection, unsupported
source blob or unrecoverable model error stops advancement. Read the retained `security-audit/blocked.json`
in that worker's state directory. Resolve findings by explicit human attestation,
not by rerunning the model until it passes. Do not change worker IDs to bypass a block.
The input, actual launcher/environment/prompt, transcript, native Codex
compaction/usage records, read receipt (for larger inputs), and durable ledger are
retained in the same directory. A pre-inspection commit-budget block from an older
planner can be archived automatically and its exact next commit replanned under
the current limits. This does not attest that commit or retry a model verdict: findings,
incomplete inspections, unauthorized tools and other planning errors remain blocked.
Only the operational failures described below can resume automatically.

For private Zulip alerts, set `TAUCETI_SECURITY_AUDIT_ZULIP_CONFIG` in the trusted
worker's environment to an absolute path to a mode-0600 JSON file:

```json
{"site":"https://leanprover.zulipchat.com","email":"BOT_EMAIL","api_key":"BOT_API_KEY","recipient_id":123}
```

Use a bot credential and the recipient's numeric Zulip user ID. The credential
and its configuration path are excluded from the isolated reviewer's environment.
Alerts include the commit range, recorded findings and local evidence paths.
Source findings and explained code-safety uncertainty send an immediate DM marked
**Human review required**. Routine unfinished turns and provider errors stay in the
logs while automatic recovery handles them. An operational pause sends a DM only
when the same cursor has remained held for at least one hour after the first
observed pause. Its separate **Operational stall** label explains that no source
finding is recorded and eligible automatic recovery continues. This durable timer
survives retries and Worker restarts; advancement starts a fresh delay. The timer
changes notifications only, preserving all blocks, evidence and recovery checks.
Alerts are sent only as direct messages; no GitHub issue is created. A durable local
receipt suppresses repeated alerts for the same blocked range and kind. Failed
delivery is retried on a subsequent audit round after at least 15 minutes, checking
the latest 100 messages in that private conversation for an earlier ambiguous delivery.
Notification failure preserves the audit block and allows bump/progress work to continue.
Without this setting, reporting remains local to the worker's evidence and logs.

Codex Luna has the native persistent goal tools enabled, with a goal to inspect
the entire immutable bundle and preserve all findings and unresolved questions.
The noninteractive `codex exec` command exits at the end of a turn, so the Worker
owns continuation. Native goal databases are copied with SQLite backup into each
successor attempt, including committed WAL contents; session indexes with temporary
rollout paths are rebuilt. The native continuation-deferral marker prevents an
active goal from starting a competing turn before `exec resume` submits the Worker's
structured-output request. Objective, status and accounting are preserved separately
from temporary provider credentials. A model's goal status never approves source or
changes the cursor.

The reader rejects skipping unread pages and reports `next_unread_page` and
`remaining_pages` after the source, before its final delivery token. When only
reading work remains, the reviewer returns `unfinished` with no findings. That is
progress, not a source verdict. The Worker also detects a premature empty-finding
pass with missing coverage, or a cleanly ended turn without a final answer. It
resumes the SAME native conversation with receipt-derived progress and retains
all notes. There are no independent partial approvals. A later passing answer
cannot discard an earlier source finding or explained code uncertainty, including
one retained only in native history.

Coverage continuations, recognized provider failures and mechanical output repairs
share at most three launches per invocation, with 5- and 10-second delays. Failed
launches and partial turns are archived under `service-error-N/`, `output-error-N/`
or `coverage-turn-N/`. Native history, reader receipts and goal bookkeeping are
durable while the subprocess runs, so a restart retains evidence written before
stdout capture finishes.

After those immediate requests, ordinary `work` recovers operational and unfinished
coverage blocks without human intervention. A durable budget waits 5, 10, 20, 40,
then 60 minutes between recovery invocations, capped at one per hour thereafter.
Reservation precedes launch and survives restarts. Every invocation rechecks hard
quota limits, admin authority, the current cursor, exact next main prefix,
immutable input, original UUID, launch policy, reader, catalog, exact prompt and
all prior judgments. Complete coverage, the final delivery token, a complete
passing judgment and confirmed ref update remain mandatory for advancement.

Known Codex capacity and request-processing retry notices are nonterminal progress
when followed by a completed turn and its answer. Unknown errors and failed turns
remain failures. If a current-protocol inspection already finished successfully
but was blocked during result handling, the Worker can revalidate its retained
answer without another model request or model quota check. The public answer must
match the native final, all prior judgments must remain clear, and complete reader
coverage and the final token must validate. Fresh admin, cursor and exact main-prefix
checks still precede advancement; the ledger records reuse of the original attempt,
and its evidence and block remain preserved until the ref update is confirmed.

Reviewed version-1 and version-2 launcher profiles can migrate in their original
native session. Version-2 coverage-only incompletes are recognized only by a closed
diagnostic grammar whose claimed unread range and final token match the retained
receipt. Their original native final and ledger record must agree; findings and
unknown prose remain blocked. This migration is a protocol rule, with no exception
for a particular commit SHA. Original evidence stays on record. Source findings,
explained uncertainty about code, unauthorized tools, unknown profiles and
mismatched evidence remain blocked. Private alerts retain their usual deduplication.

The `retry-service` and `repair-output` administrative commands remain for their
restricted historical recovery profiles; automatic protocol-v3 recovery uses the
ordinary worker loop and does not need those commands or a human attestation.

For the recorded legacy Luna failure where the final two pages were inaccessible
because the reader token was lost, an admin may explicitly continue the **same
retained native conversation** after repairing the reader:

```bash
python3 -m tauceti_worker.security_audit resume --state /path/to/worker/state/security-audit \
  --reason 'Reader-token transport repair; continue the retained inspection'
```

This command allows one administrative continuation, restricted to the exact
original recorded token-failure block. A failed continuation cannot be retried. It checks the
unchanged input digest, original commit order, current cursor and admin authority,
then restores the native conversation and read receipt. It keeps the incomplete
verdict and block on record; only a complete passing continuation and confirmed
reference update resolve the block. It cannot start a new reviewer, resume a source
finding or other incomplete review, or provide human attestation. This historical
exception does not widen ordinary automatic operational recovery eligibility. `--ignore-quota` overrides
pacing only when explicitly selected; hard limits still apply.

This phase can run alone or alongside bump/progress maintenance. It cannot use `--pr` or authoring-model overrides,
and is never enabled by `--skip` alone. `--agent codex` uses `gpt-6-luna`;
`--agent claude` uses Haiku, with ordinary subscription pacing and no expensive fallback.
It uses its own isolated launcher regardless of the ordinary host/Bubble setting.
The active GitHub credential must have repository-admin permission, checked before
spending and again before updating the cursor. New toolchain tags must be at or
behind the cursor; see [the policy](https://github.com/TauCetiProject/TauCeti/blob/main/docs/security-audit.md).

An admin initializes the protections and original audited snapshot once with:

```bash
python3 -m tauceti_worker.security_audit setup --state /path/to/private/audit-bootstrap
```

Setup is idempotent and refuses to overwrite a differing existing ruleset. Cursor
updates are admin-only, with separate non-bypassable deletion/rewind restrictions.
`v*` tag creation is admin-only, and existing tags cannot be updated or deleted.

After manually inspecting every intermediate commit in a blocked or oversized range,
an admin can record the evidence and advance with the same ancestry checks:

```bash
python3 -m tauceti_worker.security_audit attest --state /path/to/worker/state/security-audit \
  --through FULL_MAIN_SHA --reason 'Human review evidence and explanation'
```

The local ledger and blocks are operational records, not a shared authorization
service. This design trusts admins, including credentials used by other agents;
admin credentials should be kept out of ordinary authoring/review workers.

With no `--only`, a round walks the whole cascade and does the first job that
applies. `--only <task>[,<task>...]` pins it to particular kinds, and `--skip`
drops kinds from the cascade (the two combine by subtraction):

```bash
tauceti work --loop --only review     # only review open PRs
tauceti work --loop --only fix,fix-ci # only tend to our own PRs
tauceti work --loop --skip roadmap    # everything except authoring new PRs
```

Roadmap rounds steer toward one area, a subdirectory of the
[roadmap](https://github.com/TauCetiProject/TauCetiRoadmap):

- `--roadmap-only <area>` pins it. An empty value means all areas. With nothing
  set, each round picks a fresh random area, so an unpinned `--loop` roams the
  whole roadmap over time.
- `--roadmap-skip <area>[,<area>...]` excludes areas from both the random pick
  and the all-areas case. `--roadmap-only` wins on overlap.
- `--source <path-or-url>` adapts compatible material from an existing repository,
  and needs the roadmap phase enabled plus one pinned area. It is supplementary:
  the agent prioritizes the roadmap as written, then review-quality library code,
  and only then migration of the source.

```bash
tauceti work --only roadmap --roadmap-only Topology --source ../existing-library
```

Roadmap workers also avoid finer-grained targets other contributors have claimed
on the [intentions board](https://github.com/leanprover-community/intentions).
Adjust with `--roadmap-extra-identities` (logins that count as your own side) or
turn it off with `--ignore-claims`; see [the reference](docs/reference.md).
Assigned intentions carrying the maintainer-applied `administrative-hold` label are binding for
every worker, including the assignee's own workers, and cannot be disabled by those options.

### Which PRs: `--pr`

A round normally picks its own target off the queue. `--pr <n>[,<n>...]` (repeat
the flag, or pass a comma list; a leading `#` is fine) points it at particular
pull requests instead:

```bash
tauceti work --pr 412                  # whatever the cascade wants to do to #412
tauceti work --pr 412,415 --only fix   # only those PRs, and only the fix unit
tauceti work --pr 412 --dry-run        # what it would do to #412, doing nothing
```

`--pr` only ever *removes* work. It cannot make a PR actionable that the round
had already passed over, so a spent attempt budget, a peer's in-progress review,
and the branch claims all still hold: "work on these PRs"
means "of the work you were already willing to do, only this". Progress and
roadmap rounds name no existing PR, so a targeted round drops them rather than
quietly authoring something unrelated when the named PRs turn out to have
nothing to do, and it makes no GitHub writes at all about PRs you did not name —
not even the tracking issue an untargeted round files for a PR whose review
keeps erroring.

An empty or unreadable value is an error, not "no targeting": `--pr ""` and
`--pr 4 12` both stop the round rather than quietly turning it back into a
free-running worker.

That case — nothing to do — is the one worth knowing about, so the round says
why, one line per PR you named, before it exits without progress:

```
--pr: this round considers only #412, #415
  --pr #412: fix: reviews at head are all green
  --pr #415: review: a peer reviewer (codex) holds this head
```

`$TAUCETI_PR` is the environment equivalent, which is how a
[managed worker](docs/workers.md) gets one through its `env` table. Under
`--loop` the targeting is re-applied every round, so a targeted loop backs off
rather than wandering onto other work.

### Which agent: `--agent`

`--agent` is independent of `--only`, so any kind of work can run on any agent:

| `--agent` | Model | Billing |
| --- | --- | --- |
| `auto` (default) | Codex (`gpt-6.1-sol` → Luna if unavailable, high) preferred; Claude (`claude-opus-5-5`, high) fallback | subscription, paced |
| `codex` | `gpt-6.1-sol`, high effort; Luna fallback if Sol is unavailable | subscription, paced |
| `claude` | `claude-opus-5-5`, high effort | subscription, paced |
| `kiro` | `gpt-5.6-sol`, high effort by default; exact `claude-opus-5` opt-in | subscription credits, unpaced |
| `deepseek` | `deepseek/deepseek-v4-pro` via OpenRouter + [`pi`](https://github.com/badlogic/pi-mono) | pay-per-token (`OPENROUTER_API_KEY`) |
| `minimax` | `minimax/minimax-m3` via OpenRouter + `pi` | pay-per-token (`OPENROUTER_API_KEY`) |

Claude authoring and reviews both default to exact `claude-opus-5-5`, on the host and in Bubble.
Set `TAUCETI_CLAUDE_MODEL` to override reviews independently of the authoring profile.

Set a default with `TAUCETI_AGENT`. Kiro and the OpenRouter agents are unpaced
and never run on their own; you have to ask for them by name. Kiro always passes
an exact model ID and first checks that the logged-in account advertises it—its
Auto router is never used. For example:

```bash
tauceti work --agent kiro                              # exact gpt-5.6-sol
tauceti work --agent kiro --author-model claude-opus-5
```

Run `kiro-cli chat --list-models --format json` to see which exact IDs your
account currently has. `KIRO_API_KEY` is supported for headless runs; when set,
TauCeti isolates Kiro's browser-login store so the persisted login cannot take
precedence over the key.

For an explicit provider, `--author-model` and `--author-effort` override the
profile for one run. Pinning a Codex model also disables the automatic Luna
fallback. Every authoring launch prints its effective provider, model, effort,
and sandbox.

### Credit telemetry

`tauceti usage` reads Kiro subscription credits and OpenRouter balances without
sending a model prompt. It is observability only and never changes provider
selection or loop pacing:

```bash
tauceti usage --provider kiro
tauceti usage --provider openrouter --json
tauceti usage --kiro-burn-rate 2.4 --openrouter-burn-rate 1.50
```

The optional burn rates estimate rounds remaining. Kiro is queried through its
ACP `usage` command and fractional `used`/`limit` values are preserved. An
`OPENROUTER_API_KEY` reports its own usage and limit; additionally set an
`OPENROUTER_MANAGEMENT_KEY` to report account-wide purchased credits and usage.

### Which account: `--account`

TauCeti spends whatever account the agent CLIs are already logged into. If you
have several ChatGPT accounts and care which one pays, `--account` makes that
explicit:

```bash
tauceti doctor                 # shows which Codex account the credential is for
tauceti work --agent codex --account you@example.com
```

It is a check, never a switch: a credential for a different account exits the
round before spending anything. Codex only, because its credential carries the
account identity and `codex login status` will not show it. To run TauCeti on one
account while your interactive `codex` keeps another, give it a private
credential directory with `CODEX_HOME`; see
[the reference](docs/reference.md#codex-accounts).

### Where it runs: the host, or `--bubble`

Every round runs its agent directly on the host by default. It's fast, but the
agent has your full credentials and network, so keep it for trusted or local
runs.

For code and review phases, `--bubble` runs the selected agent and its checkout
inside a repo-scoped [bubble](https://github.com/kim-em/bubble) container. The
agent's git and gh traffic goes through Bubble's proxy, your `gh` token never
enters the container, only the selected agent credential is seeded, and none of
your host config crosses the boundary. The outer worker still surveys GitHub and
coordinates the round from the host. Progress-report rounds are the exception:
they always run on the host. Bubble needs an
[Incus](https://linuxcontainers.org/incus/) runtime. See
[the sandbox notes](docs/sandbox.md) for the exact boundary and requirements.

The agent's conversation transcript is normalized into readable narration,
reasoning summaries, tool activity, and results. File edits are listed by path
instead of repeating full diffs; tool inputs are bounded, and tool results over
64 KiB keep their first and last 32 KiB with an omitted-byte marker. Raw thinking
is never included. A round redirects this transcript to a timestamped file under
`logs/` and prints the path, tailing it if the agent exits non-zero. Pass
`--stream` to watch the identical transcript live instead.

## Persistent workers

Persistent workers are declarative and do not belong to a terminal session. You
describe what you want, and a manager keeps reality matching it. Nothing needs to
exist first:

```bash
tauceti workers add                        # an enabled worker1, the whole cascade
tauceti workers add reviewer --only review # a focused, named worker
tauceti workers                            # desired and actual state
tauceti workers logs --follow reviewer     # its durable console log
```

`add` writes the definition to `workers.toml`, under
`$XDG_CONFIG_HOME/tauceti/` or the platform default, and starts a manager. From
there, `enable`, `disable`, `restart`, and `remove` adjust one worker each. The
manager validates the whole file before applying it, starts missing enabled
workers, gracefully stops disabled or removed ones, restarts only definitions
that changed, and backs off repeated failures.

For fields that `add` does not expose, run `tauceti workers edit`, validate with
`tauceti workers apply --check`, then apply. Editing while the manager runs is
safe: it keeps the last valid generation if the new file fails validation. A
later `enable`, `disable`, `add`, or `remove` rewrites the file canonically and
drops comments and hand formatting.

`workers apply` starts a detached manager for the current login session. To hand
an existing manager over to a native user service that survives logout and
returns after a reboot:

```bash
tauceti workers manager-stop --leave-workers
tauceti workers service install
tauceti workers service status
```

This installs a systemd user service on Linux or a LaunchAgent on macOS. Omit
the first command when no detached manager is running.

Every worker needs a unique id, which `add` assigns for you. The id namespaces
that worker's state, checkout, review store, and logs, and isolates its mutable
agent credentials where the platform allows, so credential refreshes don't race.
Workers coordinate through GitHub rather than through each other, so adding
workers adds throughput. Ad-hoc rounds take the same id through
`tauceti work --worker-id alice`.

[The workers documentation](docs/workers.md) has the full `workers.toml` schema,
every action, the credential isolation rules, and the tmux viewer.

## Pacing against quota

`tauceti` paces Codex and Claude against their session and weekly subscription
limits with no setup beyond logging in with the official CLIs. A provider is
available only while its used percentage is strictly below the budget for the
elapsed fraction of every reported window; `--agent auto` prefers Codex, to spare
the scarcer Opus, falls back to Claude, and sleeps when neither has room. A
provider held back by the pace line wakes when the rising budget overtakes its
usage, which is normally well before the window resets. Usage it cannot read
counts as unavailable rather than free. The dashboard and `tauceti status` show
current usage and why a provider is waiting.

| Control | Effect |
| --- | --- |
| _(default)_ | The curve `60:40`: 40% of the quota by 60% of the window, then a ramp to the full quota by the reset. Holds a reserve for work that arrives late in a window |
| `--pace 0:10,50:70,90:90` | Use a different piecewise-linear `time%:budget%` curve: 10% allowed immediately, ramping to 70% by halfway and 90% at 90% of the window, interpolated between points. Usage must remain strictly below the current budget. Budgets ≥ 100 mean no soft cap; a window at 100% used still backs off |
| `--pace 0:0,100:100` | Spend at clock rate: the plain `used% < elapsed%` rule, which was the default before |
| `--ignore-quota` | Ignore soft pacing for an explicit `--agent codex` or `--agent claude`; hard limits still apply |
| `--quota-cmd CMD` | Your own pacer, run as `<cmd> <agent>`: the first stdout token is the model to run; empty output means wait |

`TAUCETI_PACE` and `TAUCETI_QUOTA_CMD` set the corresponding controls by
default. After a Claude window resets, `tauceti` may make one small request to
start its usage clock, but only after it has found work and confirmed the other
window has room.
[The quota notes](docs/quota.md) cover credential sources, the macOS Keychain,
and that bootstrap in detail.

## Claude API grants

Use explicit API billing with `--claude-billing api` and `--agent claude`.
Enable `--budget` to share local grants across workers. The
[budget design](docs/api-budget-plan.md) covers accounting and recovery.

API workers share one persistent local budget, including across worker
restarts and different checkouts. The budget controls when sessions start. It
never cuts off a running session; completed costs can take the balance negative,
and automatic grants repay that deficit before more work starts.

```bash
tauceti budget --grant 100          # add $100 to the current balance
tauceti budget --set-grant 50       # set the current balance to $50
tauceti budget --grant-rate 20      # replenish at $20/hour, even while stopped
tauceti budget                     # balance, rate, pending costs, and ledger path
tauceti budget --log                # append-only accounting history

# API key is supplied through ANTHROPIC_API_KEY or a private key file.
tauceti work --agent claude --claude-billing api --budget --loop \
  --worker-id worker1 --anthropic-api-key-file /path/to/anthropic.key
```

`--set-grant` records an adjustment rather than erasing history. It preserves
the replenishment rate and costs still owed by running sessions. Setting
`--grant-rate 0` stops replenishment; there is no accumulation ceiling or
per-session spending cap. Active sessions carry estimates of pending costs so
parallel workers do not all start against the same funds. Estimates only affect
new admissions. Funding waits happen outside the timed round.

The append-only ledger lives at
`$XDG_STATE_HOME/tauceti/budget/events.jsonl` (normally
`~/.local/state/tauceti/budget/events.jsonl` on Linux), with a macOS equivalent
and an explicit `TAUCETI_BUDGET_DIR` override. Grants, rate changes, balance
adjustments, session admissions, and cost settlements remain auditable. Unknown
costs remain visible for reconciliation and retain their pending estimates.
Workers continue whenever available funds cover the next admission; API
throttling is handled separately from funding. Local accounting uses Claude's reported cost estimates
and does not represent the actual credit balance in Anthropic Console.

Use `tauceti budget --json` for scripts. To recover a missing cost or correct a
settlement, use `tauceti budget --reconcile INVOCATION --cost USD --note TEXT`;
the correction is appended to history. A cold model runs one calibration
session before parallel admissions use learned cost estimates. Its reservation
is at least $1, or the largest of the last 20 recorded costs across all models if that is higher;
zero-cost launch failures do not establish a spending estimate. If that session
loses its receipt, another calibration session can run; missing costs never
pause the fleet merely because reconciliation is outstanding. Resuming the same
provider session waits while a previous resume is unsettled; unrelated sessions
can run. Recover the cumulative receipt before resuming that provider session;
a cost-only manual reconciliation does not restore its cumulative baseline. No local budget amount is passed as
Claude's `--max-budget-usd`. After a crashed worker, first try
`tauceti budget --recover --note "worker crash"` to recover its durable receipts
and remove stale staged keys. Costs without a receipt still require reconciliation.

Managed workers accept the same flags through `workers add`, or the
`claude_billing = "api"`, `budget = true`, and `anthropic_api_key_file` fields
in `workers.toml`. Keep the raw API key out of that file.

## Further documentation

- [Persistent workers](docs/workers.md): the `workers.toml` schema, every
  `tauceti workers` action, state on disk, and running past logout.
- [`tauceti work` reference](docs/reference.md): every flag and environment
  variable.
- [Quota and pacing](docs/quota.md): credential sources, Claude's two windows,
  and the window bootstrap.
- [Claude API grants design](docs/api-budget-plan.md): shared local
  budget, append-only ledger, and API worker integration.
- [Inside the sandbox](docs/sandbox.md): what `--bubble` enforces, Lake caches,
  and macOS credential handling.
- [Docker deployment](docs/docker.md): the unattended Compose deployment.
- [Unfinished source recovery](docs/work-recovery.md): records saved before
  checkout preparation resets, and checkouts moved aside while still in use.

The `overlap-scan` and `overlap-fix` phases maintain a shared Mathlib cursor and
adopt overlapping upstream APIs. See [Mathlib overlap maintenance](docs/mathlib-overlaps.md)
for initialization, coordination, and the live calibration results.

Run Python host helpers from this checkout with `uv run python PATH_TO_HELPER` so they
use the pinned shared Tools dependency. Update any local timer commands to use the same
invocation before upgrading this checkout.

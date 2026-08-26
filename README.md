# Tau Ceti Worker

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
uv tool install git+https://github.com/kim-em/TauCetiWorker.git

tauceti doctor                     # report the tools and credentials this host can use
tauceti                            # the dashboard: see the available work, launch it
tauceti status                     # the same survey, non-interactive (--json for scripts)
tauceti usage --json               # prompt-free Kiro/OpenRouter credit telemetry
tauceti work --only review         # one round of a specific kind of work, then exit
tauceti work --loop --only review  # a focused worker: keep reviewing (or fix / roadmap / ...)
tauceti work --loop                # fully automatic: keep picking the most useful job
```

Ctrl-C stops the current round and exits. From a clone you can run `./tauceti`
instead, a small PEP 723 `uv` shim that runs the same package; every command
below works either way, and this README writes the installed form.

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

A round does exactly one unit of work: the first of these that applies.

| Step | What it does |
|------|--------------|
| **Rebase** | Resolve one of our conflicting PRs — a genuine content conflict under `TauCeti/` after a sibling merged first (the root `TauCeti.lean` is auto-synced on `main`, so it no longer collides). |
| **Bump** | Adapt a red `bump-mathlib/` PR (the review bot opens those to move the Mathlib dependency forward) so `TauCeti/` builds against the new Mathlib. The worker never opens a bump itself. |
| **Progress** | When the global eight-hour cadence is due, update one roadmap's generated `STATUS.md` and `PROGRESS.md` through TauCetiProgress. |
| **Fix CI** | Repair one of our PRs whose `build` check is red. It cannot be reviewed until it builds, so this comes before Fix. |
| **Fix** | Address the review findings on one of our PRs: fix the code, or contest a wrong finding on its thread. |
| **Review** | Review an open PR whose head is green but not yet reviewed, with the `tauceti-review` engine. Maintenance on our own PRs takes priority so author-action work (`ci-failed` or `awaiting-author`) cannot be starved by unrelated reviews. |
| **Roadmap** | Otherwise, open a new PR advancing a [roadmap](https://github.com/TauCetiProject/TauCetiRoadmap) target. |

Merging green PRs, closing stuck ones, and de-duplicating are the repo's CI, not
the worker. A GitHub API failure aborts the round rather than reading as "nothing
to do", so a transient outage never falls through to authoring.

A review has two outputs with different roles. The head-pinned scoreboard posted
on the PR is the live verdict that auto-merge reads. Detailed run records are also
kept in a local outbox for the public TauCetiData analytics/provenance archive;
failure or lack of permission to publish that archive does not stop the posted
review from counting.

## Configure a round

Three independent dials: which work, which agent, and where it runs. Combine
them however you like.

### What work: `--only`

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

### Which agent: `--agent`

`--agent` is independent of `--only`, so any kind of work can run on any agent:

| `--agent` | Model | Billing |
| --- | --- | --- |
| `auto` (default) | Codex (`gpt-5.6-sol` → Terra if unavailable, high) preferred; Claude (`claude-opus-5`, high) fallback | subscription, paced |
| `codex` | `gpt-5.6-sol`, high effort; Terra fallback if Sol is unavailable | subscription, paced |
| `claude` | `claude-opus-5`, high effort | subscription, paced |
| `kiro` | `gpt-5.6-sol`, high effort by default; exact `claude-opus-5` opt-in | subscription credits, unpaced |
| `deepseek` | `deepseek/deepseek-v4-pro` via OpenRouter + [`pi`](https://github.com/badlogic/pi-mono) | pay-per-token (`OPENROUTER_API_KEY`) |
| `minimax` | `minimax/minimax-m3` via OpenRouter + `pi` | pay-per-token (`OPENROUTER_API_KEY`) |

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
profile for one run. Pinning a Codex model also disables the automatic Terra
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

## Further documentation

- [Persistent workers](docs/workers.md): the `workers.toml` schema, every
  `tauceti workers` action, state on disk, and running past logout.
- [`tauceti work` reference](docs/reference.md): every flag and environment
  variable.
- [Quota and pacing](docs/quota.md): credential sources, Claude's two windows,
  and the window bootstrap.
- [Inside the sandbox](docs/sandbox.md): what `--bubble` enforces, Lake caches,
  and macOS credential handling.
- [Docker deployment](docs/docker.md): the unattended Compose deployment.

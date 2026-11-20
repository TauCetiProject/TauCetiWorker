# Persistent workers

Worker data (`state/`, `checkouts/`, `logs/` and `host-repositories/`) lives in
one data root. An installed package (`uv tool install`, pip) uses
`$XDG_DATA_HOME/tauceti`, else `~/Library/Application Support/tauceti/data` on
macOS, else `~/.local/share/tauceti`, so upgrading or reinstalling the code keeps
it. A Fleet runtime uses `<fleet data>/worker-data/root` (normally
`~/.local/share/tauceti-fleet/worker-data/root`), shared by every runtime
revision; it keeps `~/.local/share/tauceti` only while that holds worker state
and the fixed root does not. A source checkout keeps it beside the code,
gitignored. For the legacy TauCeti project, `TAUCETI_LEGACY_DATA_ROOT` selects
another absolute directory; round, verification and cleanup subprocesses inherit
it, and bundled scripts and prompts remain with the code. Changing the location
does not move data: the cleanup collector deletes data an earlier installation
left in its package directory, and on Fleet hosts other runtimes' roots, once no
worker or round holds their locks.

A data root inside a virtualenv looks disposable, so `tauceti doctor` fails and
a worker loop refuses to start when it would create data there. Data already
inside one only draws a warning; move it out by renaming each part into the new
root and leaving a symbolic link at the old name. While workers run, swap the
part with a link created beside it in one step (`mv --exchange`, coreutils 9.5),
then rename the swapped-out directory into the new root: a separate rename and
`ln -s` leave a moment where a worker can recreate an empty part at the old name. The collector treats a root
whose parts are symbolic links into another as an alias: it collects the target
once, through the selected root, and never stages or deletes it through the link.

## Fleet admission and reporting

TauCetiFleet can supervise a separate worker configuration without adopting this
account's existing manager. Its `TAUCETI_FLEET_ADMISSION_FILE` names a private,
controller-owned version-1 JSON policy with `expires_at` and an allowed `workers`
list. The controller refreshes it while running. Missing, malformed, expired, or
clock-inconsistent policies prevent new rounds and paid Claude API admissions;
an already-started agent session runs to completion. Workers report
`waiting-capacity` while draining instead of being terminated. Workers without
the environment variable behave as before.

Each round also records, in the worker's runtime status, the authoring
backpressure it enforces: `authoring` holds the open non-draft PRs it counts as
its own on every healthy source (`sources`, `count`), the `limit`, whether roadmap
authoring is `blocked`, the roadmap `scope` the count covers and its `skip`
list, and `observed_at`. The count comes from that round's own survey, so it is
current on any forge. A starting loop clears the previous report.

`TAUCETI_FLEET_MACHINE_ID` and `TAUCETI_FLEET_EVENTS` opt into a private JSONL
provenance journal. Successful PR creation and GitHub mutations record machine,
worker, repository and PR identity. Branch pushes record assistance when the PR
identity is known. These observations are statistics only: they never authorize
CI, reviews, merges, cache publication, or another GitHub mutation. No request
body, command line, credential or agent prompt is retained. Event reporting is
best effort and cannot fail a successful publication.

Fleet's admission policy may also contain advisory `roadmap_focus`: `areas`
(directory names), `workers` (designated slots), and optional `fractional_worker`
and `fractional_percent` for a fractional slot. Designated workers select an
area before their survey from the intersection of that list and their eligible
live areas, then run the normal maintenance, review, and authoring cascade within
that area. The fractional slot takes that draw with the supplied integer
percentage each round. Other workers retain the full random draw, and a worker with a
[target list](target-lists.md) draws from its list instead. Explicit `--roadmap-only` and
`--roadmap-skip` settings retain precedence; the usual maintenance and review
priorities apply within the chosen area. Stale/malformed focus or no remaining
eligible focus areas falls back to ordinary selection. Fleet controls this live through
`tauceti-fleet focus HOST --percent 20 --areas PDE,SymplecticGeometry`; changing
it does not restart workers or change admissions. The share describes focused
slots, not runtime or output quotas. Focused slots retain the shared account-wide
open-PR limit. If that limit prevents authoring and the selected area has no
available maintenance, the slot falls back to the ordinary global cascade.

Fleet installers can query `_fleet-protocol` to verify this integration before
starting workers. The response is local, contains no credentials, and launches
no agents. `"graceful_restart": 1` there (and in the manager's `ping` reply)
means a manager `restart` request waits for the worker's current round; see
[Restarts and rolling deploys](#restarts-and-rolling-deploys).

On Linux, Fleet sets `TAUCETI_WORKER_SLICE` to keep each worker's entire process
tree in an independent systemd scope, sharing the existing pool's memory and CPU
limits. A manager-service restart preserves these workers and adopts them again.
Explicit Fleet updates stop the worker slice, including surviving scopes; a
manager-service stop alone is not a whole-fleet stop. `manager-stop` still stops
workers unless `--leave-workers` is selected. Scope setup errors are reported as
degraded isolation and work continues in the same wrapper process. Journal and
status storage are never required for recovery. Standalone and macOS managers
retain their existing launch and shutdown behavior.

Persistent workers are declarative. You describe the workers you want in
`workers.toml`, and a manager process reconciles reality against that file:
starting missing workers, stopping removed ones, restarting changed ones, and
backing off repeated failures. They do not belong to a terminal session.

## Source selection

Worker definitions select the same sources as `tauceti work`. Existing definitions
keep their GitHub-only defaults. For a Forgejo worker, set:

```toml
github = false
forgejo = true
new_pr_forge = "forgejo"
```

Set both source booleans to `true` to maintain PRs from both sources. New PRs use
`new_pr_forge`, independently of maintenance selection. Configure all three fields
for native authoring; without an explicit destination, new PRs still default to GitHub.
These fields are available in `workers.toml`. Credentials use the existing
private Forgejo token-file configuration. Source changes wait for the worker's
current round to finish; other workers retain their running processes and settings.

## Locally verified publications

With `self_tested = true` and `TAUCETI_VERIFY_HELPER=1`, the worker applies
`self-tested` after an authoring or repair round publishes ordinary mathematics
entirely under `TauCeti/`. Every verification phase must succeed in that round,
and the receipt's source inventory must exactly match the published commit.
Pin changes, infrastructure changes and full-lint/lint-repair requests do not
qualify. This is the same eligibility check for GitHub and Forgejo.

Both forges use the same add-label flow through their source adapters; no picker
or separate status publication is required. Provision the `self-tested`
repository label on Forgejo before enabling this route. This rollout does not
change publisher grants.

The label is a persistent author declaration. Keep it through fix/rebase pushes
after successful verification; remove it when verification fails or is
incomplete. Authors can apply it themselves after verification, and the worker
also applies it when it finds the complete matching receipt after a round.
An unavailable or invalid receipt never produces an automatic label. Ordinary
PR-CI eligibility is checked independently at admission.

Forgejo does not currently run an individual Actions build for each PR. Reviews
still run, and every mixed merge-queue candidate requires its full CI build and
audits before landing, including self-tested members. Source statuses cannot
replace that candidate CI. For live deployments, preserve every per-worker
`env` entry, including any `TAUCETI_ROUND_WORKTREES=0` opt-out; the verification
helper can also be enabled in the worker manager's service environment.

## Dependency bumps

Bot-authored `bump-mathlib/` PRs hosted on TauCeti are repaired until they land.
Their build, rebase, review-error, review-fix and contest attempts are not permanently
capped. Existing loop backoff, quota pacing, branch claims,
and explicit holds still apply. Workers checkpoint coherent partial source repairs
before their round deadline; full CI, audits, and current-head reviews authorize merging.

Bump work comes before all other work in every round. Each enabled stage's
candidates on a bump PR (conflicts, a red build, review findings and pending
reviews) are offered first, in stage order, ahead of progress reports and
every other PR, and a bump review does not wait for its previous reviewer's
first-refusal window. Main lands large batches every half hour or so, so a bump
that waits its turn goes stale before it can be admitted. No worker is reserved
for bumps: one branch claim serializes edits anyway, and every worker whose
`only` list includes the stage picks the bump first.

After a host round pushes a new bump head, the worker runs the full local
verifier (`tauceti verify`, bump policy) on that exact commit with the same
build-cache view the agent used. If it succeeds and GitHub still reports that
commit as the PR head, the worker applies `self-tested`. Review and queue admission
validate eligibility directly (the labeler's team membership, bump-only paths
and the real `bump-guard` validation), without waiting for an Actions receipt or
the ordinary PR build. The agent's
own claims are never consulted. Bubble rounds, a dirty checkout, a moved head,
or a failed or incomplete verification (capped at 45 minutes) leave the PR on
ordinary CI, which the push has already started. The label persists through pushes and fix rounds; withdraw it if local
verification fails or is incomplete. Set
`TAUCETI_BUMP_SELF_TESTED=0` in a worker's `env` to turn this off.

## Rebase retry budget

For genuine Git conflicts, a tended PR gets three rebase attempts between verified progress checkpoints.
The ordinary survey records a checkpoint when the current head has a successful
`build` status, GitHub reports it mergeable, and it has no `needs-rebase` label.
A distinct green head can renew the allowance once; rebuilding or repeatedly
observing the same head cannot.
Unverified pushes, comments, agent exit codes, failed or pending CI, unknown
mergeability, drafts and hold labels do not renew it.

Trusted merge-sweep handoffs retain the existing three-attempt per-PR cap. A
handoff can start from a branch that is already green and mergeable, so another
green push does not establish that the reconciliation succeeded. A missing or
failed handoff-comment read never bypasses the `needs-rebase` label's hold on
budget renewal.

The evidence comes from the existing survey, without extra GitHub reads or agent
sessions. Each worker keeps its checkpoints with its existing counters. A round
charges and refunds the allowance it selected even if another survey observes
new progress while that round is running. Old integer counter files remain
intact; PRs with no verified checkpoint keep their existing allowance. This is
a per-worker bound, as before, not a fleet-wide attempt limit.
Unreadable or malformed checkpoint/counter evidence suppresses rebase work with
a diagnostic reason. Counter and checkpoint writes use atomic, synced replacement.

## Quickstart

Nothing needs to exist first. `workers add` creates the config, writes the
definition, and starts a manager:

```bash
tauceti workers add                        # an enabled worker1, the whole cascade
tauceti workers add reviewer --only review # a focused, named worker
tauceti workers                            # desired and actual state
tauceti workers logs --follow reviewer     # its durable console log
```

Day-to-day controls:

```bash
tauceti workers disable reviewer   # persist desired stopped state
tauceti workers enable reviewer    # and start it again
tauceti workers restart reviewer   # after its current round, without changing desired state
tauceti workers remove reviewer    # drop the definition and stop it
```

## Editing the file directly

`workers add` cannot express every field, and it rewrites the file in canonical
form, dropping comments and hand formatting. To keep those, or to set a field
`add` does not cover, edit the file and apply it:

```bash
tauceti workers edit          # $VISUAL, else $EDITOR, else vi
tauceti workers apply --check # validate without reconciling
tauceti workers apply         # reconcile, starting a manager if needed
```

`workers edit` does not start a manager on its own; `workers apply` is the
explicit follow-up. A later `enable`, `disable`, `add`, or `remove` normalizes
the file again, so comments do not survive one.

A small configuration might look like this. The repository also includes
[`workers.toml.example`](../workers.toml.example).

```toml
version = 1

[[workers]]
id = "worker1"
enabled = true

[[workers]]
id = "worker2"
enabled = true
agent = "codex"
only = ["rebase", "review"]
ignore_quota = true
```

You can edit `workers.toml` while the manager is running. It reloads the file
each cycle. If your edit does not validate, the manager keeps the last good
generation, leaves running workers alone, and says so once:

```
tauceti workers: invalid configuration; keeping last good generation: ...
```

## Where the config lives

The first of these that is set wins:

| Source | Path |
| --- | --- |
| `tauceti workers --config PATH` | exactly that file |
| `$TAUCETI_WORKERS_CONFIG` | exactly that file |
| `$TAUCETI_CONFIG_HOME` | `$TAUCETI_CONFIG_HOME/workers.toml` |
| `$XDG_CONFIG_HOME` | `$XDG_CONFIG_HOME/tauceti/workers.toml` |
| macOS default | `~/Library/Application Support/tauceti/workers.toml` |
| otherwise | `~/.config/tauceti/workers.toml` |

`--config` belongs to `tauceti workers` itself, not to the action, so it goes
before the action name: `tauceti workers --config ./workers.toml apply`.

Writes take an `flock` on a sibling `workers.toml.lock`, then replace the file
atomically, so concurrent CLI and dashboard mutations serialize instead of
clobbering each other.

## `workers.toml` reference

The file supports two top-level keys. `version` is required and must be `1`;
`workers` is an optional array of tables and defaults to an empty array. Any
other top-level key is an error, as is any unrecognized field inside a
`[[workers]]` table. Duplicate ids are rejected.

| Field | Type | Default | Notes |
| --- | --- | --- | --- |
| `id` | string | required | `[a-z0-9-]+`, at most 40 characters; namespaces the worker's state, checkout, review store, and logs |
| `enabled` | bool | `true` | Desired running state. `false` stops the worker without forgetting it |
| `agent` | string | `"auto"` | `auto`, `codex`, `claude`, `kiro`, `deepseek`, or `minimax` |
| `github_account` | string | unset | GitHub login to select and verify for this worker. Independent of the agent account; changing it restarts the worker. Requires that account's stored gh credential or a matching environment token |
| `max_open_prs` | positive integer | `32` | Open-PR cutoff for roadmap authoring. CLI `--max-open-prs` and `TAUCETI_MAX_OPEN_PRS` also configure it. Science-account workers use `128`. |
| `github_state_url` | string | public Tau Ceti mirror | Shared GitHub state service. Clients use its newest available data, with direct fallback on unavailable coverage or transport. See [service operations](../services/github-state/README.md). |
| `github_state_token_file` | string | unset | Private client-token file for the service; installation credentials stay on the server. |
| `only` | string list | `[]` | Work phases: `rebase`, `bump`, `progress`, `fix-ci`, `fix`, `review`, `roadmap`, `lint-repair`, `overlap-scan`, `overlap-fix`. Empty means the automatic cascade. Set `TAUCETI_OVERLAP_BATCH_COMMITS` through `env` to override the calibrated 160-commit scan batch. |
| `claude_billing` | string | `"subscription"` | `"api"` requires `agent = "claude"`; API mode bypasses subscription quota polling |
| `budget` | bool | `false` | Admit API sessions through the shared local grant ledger; active sessions are never cut off by funding |
| `anthropic_api_key_file` | string | unset | Private key-file path for API mode; otherwise use `ANTHROPIC_API_KEY`; never store a raw key in config |
| `sandbox` | string | `"host"` | `host` or `bubble`. Progress-report rounds always run on the host |
| `ignore_quota` | bool | `false` | Skip soft pacing. Provider hard limits still apply; an `auto` worker cannot launch with this enabled |
| `auto_refresh` | bool | `false` | Renew this worker's Claude access token when it expires, instead of parking until a human runs `claude`. Only safe when nothing else uses the same credential file — the refresh token is single-use. See [quota and pacing](quota.md) |
| `roadmap_only` | string | unset | The single roadmap area for roadmap rounds. `""` means all areas; unset means a fresh random area each round |
| `roadmap_skip` | string list | `[]` | Roadmap areas to exclude. `roadmap_only` wins on overlap |
| `roadmap_targets` | string | unset | Absolute path of an operator [target list](target-lists.md). Roadmap rounds author only its open items whose prerequisites have landed, one claimed item per round. Owned marked pull requests also take priority within each existing maintenance/review stage. Several workers may name the same file |
| `roadmap_extra_identities` | string list | `[]` | Extra GitHub logins whose claimed intentions count as this worker's own |
| `extra_roadmap_prompt` | string | unset | Operator guidance rendered into the roadmap prompt, steering target choice within the designated roadmap. Not blank, at most 4096 characters; other phases ignore it |
| `respect_claims` | bool | `true` | Whether to avoid intentions others have claimed |
| `source` | string | unset | Supplementary repository directory or URL. Requires `roadmap` in `only` and a non-empty `roadmap_only` |
| `author_model` | string | unset | Exact authoring model. Requires an `agent` other than `auto` |
| `author_effort` | string | unset | Authoring reasoning effort for Codex, Claude, or Kiro. Requires an explicit `agent` |
| `pace` | string | unset | Pacing curve as `time%:budget%` points, for example `0:10,50:70,90:90`; rejected by `apply --check` if malformed |
| `pace_claude`, `pace_codex` | string | unset | Pacing curve for that provider's windows only, in the form `pace` takes and checked the same way. A provider without one follows `pace`; meant for an `auto` worker whose two subscriptions should be spent at different rates |
| `stream` | bool | `false` | Keep the agent transcript in the console log instead of a separate file |
| `isolate_home` | bool | `false` | Force credential isolation for the id `default`; every other id already enables it |
| `restart` | string | `"always"` | `always` after any exit, `on-failure` after a nonzero exit, or `never`; explicit restart and re-enable still work |
| `env` | table of strings | `{}` | Extra environment for this worker's process tree, for settings with no flag of their own. Values must be quoted strings, names POSIX-portable, and the table at most 16 KB. The variables the worker sets itself (`TAUCETI_MANAGED`, `TAUCETI_LOG_FILE`, `TAUCETI_PARENT_PIPE_FD`, `TAUCETI_DATA_HOME`, and the runtime-status path) are rejected. **Not a secret store** — see below |

Each pacing field overrides only its matching environment variable. An inherited
`TAUCETI_PACE_CLAUDE` still takes precedence over `pace`; use `pace_claude = ""`
to clear that override and follow the global curve. The same rule applies to
Codex. `workers status` shows the running worker's captured effective curves;
stopped workers and pending definitions show configured curves before inheritance.
API-billed workers reject subscription pacing from the `env` table and pacing
fields during configuration validation. Inherited settings are rejected in the
child before credentials are created, so the viewing shell's environment cannot
invalidate other workers' configuration.

The manager fingerprints each definition. It stops a worker when `enabled`
becomes false and restarts an enabled worker when any other field changes,
without disturbing unchanged workers. Like `workers restart`, a changed
definition takes effect after the worker's current round finishes, never by
killing it; disabling or removing a worker stops it at once.

`env` exists for running one worker differently from its peers when the
difference has no flag: an A/B of a build setting, for instance. It is part of
the fingerprint, so editing it restarts that worker and leaves the others alone,
and both `workers status` and the dashboard name the variables it sets, so the
odd worker out is visible rather than mysterious.

Put no secrets in it. The values are stored in plain `workers.toml`, and the
whole worker definition is handed to its runner on a command line, where any
process running as you can read it. Credentials belong in the provider
mechanisms `tauceti` already uses (`CLAUDE_CONFIG_DIR`, `CODEX_HOME`,
`KIRO_API_KEY`), which keep them in files rather than argv. Status output prints
only the variable names for the same reason.

Every worker enables `LAKE_ARTIFACT_CACHE=1` and
`LAKE_RESTORE_ARTIFACTS=1` by default. The second setting belongs with the first:
with the artifact cache writable, Lake may keep a build product in its store
instead of the build directory, and TauCeti's audits (`lake exe axioms`, `lake
exe module-system`) resolve `.olean`s through the Lean search path. Restoring the
copy keeps them working, and measured on a 1500-declaration module, returning to
a previously built state still fell from 4s of recompilation to 0s of restore.
An explicit value in a worker's `env` table overrides either default.

The trade is disk: old build generations remain in the store, and Lake supplies
no selective eviction — `lake cache clean` empties it all or nothing. TauCeti
records the toolchain on canonical `main` for each worker and clears that
worker's default Lake cache when the pin changes. The first observed pin only
seeds that record. Worker-private build/cache generations also expire after
72 hours and are recycled at the next round boundary, even when a worker keeps
completing short rounds. An operator-supplied external `LAKE_CACHE_DIR` is never
deleted automatically.

### Shared artifacts and storage cleanup

Private generated data is recycled after 72 hours at an ordinary round boundary,
under the worker's existing round lock. Storage age does not terminate rounds;
configured deadlines and owned-process-group cleanup continue to apply. Cleanup
requires no PID namespace or containment marker/proof. An active, mounted or
uncertain candidate is skipped locally and retried later, without pausing other
cleanup or worker progress.

`tauceti cleanup --dry-run` reports eligible generated paths without changing files
or stopping processes. Space-pressure snapshot collection may stop earlier in a
real run once headroom recovers; candidate counts do not estimate reclaimed bytes.
`tauceti cleanup` performs reclamation. Both discover retired
workers from local storage, without GitHub requests. A live loop recycles its own private cache generation under its round lock;
the hourly collector skips that loop's workspace whenever its instance-lock file
is open and never takes that lock itself. Rounds are all-or-nothing: whatever an agent pushed is
on the remote, and anything unpushed is discarded with the round's checkout, so an inactive
checkout is removed in full once it is idle. Uncertain candidates (a live process using the tree,
a mount inside it) are skipped while unrelated work continues.
Credentials, review outboxes, usage journals and budget accounting are preserved. Existing installations get one 72-hour grace
period when their generation marker is first recorded, including checkout removal.
Without local process-file observations, diagnostic candidates are skipped; private
cache recycling can still run at the worker's own safe boundary.

Cleanup also expires closed diagnostic `.log` files, inactive verification attempts,
and unused immutable Mathlib snapshots in an existing login-wide pool after three
days. Snapshot GC uses existing reader leases; cleanup does not create an absent
pool or enable sharing. The default Mathlib archive pool now lives under the TauCeti-owned build-cache root
and is collected too. Operator-selected external caches and the old interactive
`~/.cache/mathlib` directory remain outside the collector.
Default session logs rotate daily even while a loop keeps running, without
redirecting helpers' result pipes or files. Diagnostic age is based on last write;
private workspace age is a generation timestamp, not dependency file modification time.
A partially blocked recycling attempt starts the next generation for other caches;
interrupted deletions retry without removing a younger replacement. Closed logs
and inactive verification attempts are collected even while the worker's round lock
is held, using their own open-file checks, leases and pending references.

On Linux, `tauceti cleanup --install-timer` installs a persistent hourly systemd
user timer at low CPU/I/O priority, including a catch-up run after downtime. It is
scoped to the selected project's storage roots; it does not discover other projects
from worker configuration files. There is no macOS cleanup schedule in this change;
private cache generations still recycle at round boundaries there. Installing the Linux
worker service installs this timer too; existing services can install it separately
without restarting workers. Cleanup uses one collector lock and resumes interrupted
directory deletions. Directed pressure requests prefer private
artifact archives and prefer workers without active rounds.
If every worker is busy, one is still selected for reclamation at its next safe boundary.
Ordinary pressure preserves young build trees. At critical headroom, workers retain their
independent emergency reclamation at safe round boundaries, including throughout the existing
headroom hysteresis band; a missing collector or damaged request cannot suppress that path.
A concrete out-of-space error during cache preparation also
unlinks closed diagnostics before the existing single retry, without synchronously
archiving source or recursively collecting other workers' workspaces. Further failures follow normal
worker backoff. Byte and free-space thresholds also trigger collection before the age limit.
Removing the worker service leaves the cleanup timer installed so retired workers
can still be reclaimed. To remove that schedule explicitly, run
`systemctl --user disable --now tauceti-cleanup.timer`.

The last cleanup result (counts, at most 100 sample paths per category, failures and
remaining free bytes) is included in prepared activity data. Reading status does not
scan storage or contact GitHub. Shared Mathlib rollout remains off; this collector
does not change its policy or complete the natural rollout gates in #311.

Host preparation downloads CI metadata into a fresh temporary Lake cache, expands verified
archives into the machine pool, and populates each worker's private native Lake cache.
Unchanged artifacts can then be restored by Lake without repeated decompression. Publication
checks archives and extracted files with the selected Lean toolchain's own content hash.
Mutable output/revision metadata is copied, while immutable artifacts use hard links where
possible. Recompilation unlinks outputs before replacing them. Unsupported toolchains,
contended entries and unavailable shared storage use ordinary private caches automatically.
Full-project verification and Lake's build-input invalidation remain enabled.

Each preparation decodes the fresh download mappings once and reuses them through transfer
and expansion. Private metadata is checked again on the next preparation; there is no
persistent readiness marker. Hydration writes only changed mappings and missing artifacts.
Archive seeding skips duplicates, and an archive removed concurrently by pool collection
is an individual cache miss for Lake to fetch. Native fetch arguments and Mathlib snapshot
keys/inventory checks are unchanged.

A worker's build tree outlives its rounds, so it can hold modules an earlier round compiled
locally. When CI has published outputs for the same input hash and they differ from the
local ones, Lake would still trust the local trace. Locally built bytes need not match CI's,
and every module downstream would then miss the cache and compile locally. After hydration,
preparation therefore removes each such module's trace (and its `.hash` sidecars), and Lake
restores CI's outputs by hard link. Modules CI has not published are the agent's own work and
are left alone. The receipt counts `ci_disagreeing_traces`, `ci_disagreeing_removed` and
`ci_disagreeing_skipped` (deferred because a file was busy).

#### Waiting for a matching TauCeti cache

`lake cache get --max-revs=N` installs the newest published revision in the last N commits.
When CI has not yet published a commit that changed dependencies (a Mathlib bump, say), that
revision predates the change, none of its outputs apply, and the round would compile all of
TauCeti, which cannot finish within the round limit. Host agent rounds (roadmap, overlap-fix,
and the PR rounds fix, fix-ci, rebase, bump and lint-repair) therefore check the cache before
the agent starts. The revision Lake installed is the first commit in `git rev-list HEAD -n N`
whose revision map is in the round's download directory, which is the same rule Lake follows
(maps seeded from the pool included). Its `lake-manifest.json` package revisions and
`lean-toolchain` are compared with the checkout's dependency files as prepared. A rebase round
prepares with main's files, so main's cache is the one it needs. For a PR round, the main
revision fetched alongside also counts. If either matches, the round proceeds. A fallback with
the same dependencies is logged as a same-dependency fallback and only the commits in between
rebuild.

Otherwise the round ends as `waiting-build-cache` (exit 79): no failure, no attempt charged,
downloaded and hydrated outputs kept, and the worker selects other work after 5 seconds.
`tauceti workers status` shows the state, the commit and the recheck time. The round writes
`<pool>/cache-waits/<commit>-<dependencies>.json`, keyed by the head and the dependency files it
prepared, with a retry time five minutes ahead. It records the stage and the revision whose map
would serve those dependencies: the head for its own, main for main's (a rebase round). Until the
retry time, other workers on the host skip the same stage on that head, and skip roadmap and
overlap-fix while a wait on main is recorded (roadmap reports `waiting-build-cache` itself, since
it is the last candidate). A fix of a head whose rebase is waiting is not held. At most every
30 seconds per host, a selecting worker sends an anonymous `HEAD` request for that revision's
map, `<TAUCETI_CACHE_REVISION_URL>/TauCetiProject/TauCeti/tc/<toolchain>/<revision>.jsonl`, with
the prepared toolchain mangled as Lake does (`leanprover--lean4---v4.36.0`); a 200 removes the
wait. When the retry time passes, the next round to select that work claims the check for two
minutes, together with its own later candidates, and goes through. Only cache preparation
re-arms the retry time, so a round that claims and then declines lets the claim lapse for the
next one. A successful check on main clears every wait on main. Any error reading or writing
markers, or probing, lets work proceed, including the round that could not record its wait. A marker
that exists but cannot be read is left alone rather than restarted. At most once an hour per
host, selection deletes every marker whose first wait is more than seven days old, whatever its
head or stage; released PR waits are kept until then, so a spent grace is not granted again.

A wait older than an hour logs one warning per host and commit, naming the URL CI should have
published. Rounds on main wait without limit: main always gets a cache, and building anyway
only occupies the host. A PR whose own head changes dependencies relative to main only gets a
cache once CI builds that PR, which may never succeed, so after two hours of waiting its rounds
proceed on the fallback cache as before, letting the repair agent work on the PR. Bubble rounds,
bump attestation and the queue's repair service do not wait.

The existing pool status receipt also records mapping/seed/install counts and per-phase
elapsed and CPU seconds for seeding, fetching, transfer, expansion/hydration, and Mathlib
view preparation. CPU includes the preparator and reaped children. Phases can be nested,
so their durations must not be added together. These measurements never control admission.

The pool defaults to `~/.cache/tauceti/builds` under the login user's home. Configure
`TAUCETI_BUILD_CACHE_ROOT` before installing services to use another filesystem;
`TAUCETI_SHARED_ARTIFACTS=0` disables expanded-artifact sharing. Explicit Mathlib/Lake
cache overrides are preserved. Only directories carrying a valid TauCeti ownership record
are collected; an arbitrary nonempty directory is never adopted or deleted.

Every round works in its own linked worktree (a host opts out with `TAUCETI_ROUND_WORKTREES=0` in
its `workers.toml` `env` table) of one bare host clone,
`<state>/host-repositories/TauCeti.git` (override with `TAUCETI_HOST_REPOSITORIES`). The worktree
sits at the worker's usual checkout path, detached at `origin/main`; the host clone is fetched at
most once a minute under `host-repositories/TauCeti.lock`. At round end the worktree is removed
with anything unpushed in it; its `.lake` waits beside it as `checkout.lake-between-rounds`. A
crashed round's worktree is removed by the next round on the host once its worker's round lock is
free. On its first round each worker deletes its old permanent checkout. Shared Git config that could run code for every
round (hooks, fsmonitor, filters, aliases, credential and URL rewrites, includes) is cleared at each
round. Turning the flag off again is safe: the next round finds a worktree of a clone it does not
manage, removes it, and clones an ordinary checkout as before.

The collector measures allocated bytes across shared and worker-private caches, counting
hard-linked inodes once. Its eviction target defaults to 25% of filesystem capacity;
`TAUCETI_CACHE_MAX_GIB` sets an explicit target instead. Pressure collection begins when
that target is exceeded or free space falls below 15%, and aims to restore 20% free space.
Entries use explicit last-reuse timestamps. Unused entries expire after 72 hours; abandoned
publication/download staging can be removed after four hours when its publisher lock is free.
After pool eviction, the collector can request private-store reclamation from one older worker
with substantial unused artifacts. This runs at an ordinary round boundary, keeps the current
build tree warm, and has a 12-hour per-worker cooldown. Live build artifacts are protected;
a target larger than the reclaimable data does not trigger a fleet-wide cache wipe.
Mathlib's own `cache clean` runs at most daily in the private archive store, removing stale links
for dependency trees no longer in use so those archives cannot be perpetually re-promoted.

Below 5% free space, new rounds enter `waiting-storage`, without consuming repair attempts
or escalating failure backoff. Existing rounds continue; admission resumes automatically above
15% free. The existing cleanup service is requested asynchronously under pressure, with a
five-minute request cooldown, in addition to its hourly schedule. No root, GitHub requests,
whole-fleet drain, or human-resume action is needed.

Cleanup and expanded-artifact metrics are prepared in activity data: managed and live-worker-held
bytes, remaining free space, observed net space recovered, cleanup errors, and latest per-worker
expanded/reused artifact counts and preparation time. `status` reads prepared data without
walking cache trees. Space held by active work or unrelated programs cannot
be reclaimed to enforce an eviction target; this is not a filesystem quota. Usage/accounting
records and credentials are never disposable cache data.

The collector also reports storage three ways, because hard-linked files still count in `du`:
named bytes (every link counted, as `du --count-links` would), bytes allocated in unique inodes,
and filesystem free space. `status` prints all three; checkout size alone proves nothing.

### Per-round private-bytes cap

A host round's private bytes are the bytes of files whose links all lie inside the round's own
writable roots: its checkout (including `.lake` and the shared-Mathlib overlay upper), the
worker's `LAKE_CACHE_DIR` and `MATHLIB_CACHE_DIR`, and a private `TMPDIR`. A file hard-linked to a
shared store is not private; a file written, unpacked or copied there is. A walk before the agent
starts records the baseline the round inherits. While the agent runs, a monitor thread measures
again every 30 seconds, and sooner when the worker scope's cgroup `io.stat` write bytes jump. Each
pass lstats every directory but rereads only directories whose mtime changed, a rotating eighth of
the rest, and files changed during the round; the pass after the agent exits is a full walk.
Rounds get no private `TMPDIR` yet, so writes under `/tmp` are not measured. Filesystem quotas are
not used: hard links fail
across XFS project IDs and bind mounts, which would make Lake copy instead of link.

| Setting | Values | Default |
| --- | --- | --- |
| `TAUCETI_ROUND_PRIVATE_BYTES`, or `mode` | `off`, `report` (measure only), `enforce` | `off` |
| `TAUCETI_ROUND_PRIVATE_CAP_GIB`, or `cap_gib` | new private GiB allowed per round | `6` |
| `TAUCETI_ROUND_PRIVATE_SCAN_SECONDS`, or `scan_seconds` | seconds between passes | `30` |

The environment overrides the per-host file `round-storage.json` in the workers state directory
(`~/.local/state/tauceti/workers/` on Linux), for example `{"mode": "enforce", "cap_gib": 6}`.
Each round reads it again, so enabling a host needs no restart. An invalid value fails the round.

In `enforce`, a round whose new private bytes exceed the cap has its agent's process group killed.
Once the group is reaped, its checkout is deleted without saving anything, through the ordinary
retention removal, which never follows links or crosses filesystems. A checkout with a mount
inside, or one that cannot be deleted, is renamed to a hidden `.<name>.abandoned-<id>` sibling for
an operator to remove. The round ends as no progress, and the PR attempt stays charged; a queue
repair task fails. While enabled, round start also checks the leased shared Mathlib entry against its
`ready.json` inventory. A mismatch stops the round before the agent starts and records a shared
Mathlib safety stop; it never deletes through the shared path. `report` logs the same findings and
keeps the round.

Each measurement is published as `round_storage` in the runtime status and as `private_storage`
in the round's `resources.jsonl` record. A finished invocation also writes
`state/<worker-id>/round-storage.json` and a `round_storage` activity event. Bubble rounds,
verification jobs and review rounds are not measured.

The plan is to enable `enforce` on a host once authoring, verification and review rounds
keep completing, deletion keeps progressing, and private copies do not regrow. Until shared
Mathlib is required and TauCeti outputs are relinked to one canonical copy, rounds that rebuild or
fetch Mathlib privately exceed the cap by design. Run `report` first to see which rounds would be
killed. The baseline walk costs a few seconds per round for a fully built checkout, and memory of
about 8 bytes per file plus a dictionary entry per hard-linked file.

## `workers add` flags

`add` takes an optional id, defaulting to the next free `workerN`, and writes an
entry with `enabled = true`.

| Flag | Sets |
| --- | --- |
| `worker_id` | `id`; omit it for the next free `workerN` |
| `--agent AGENT` | `agent` |
| `--only TASKS` | `only`, as a comma-separated list |
| `--sandbox {host,bubble}` | `sandbox` |
| `--claude-billing {subscription,api}` | `claude_billing` |
| `--budget` | `budget` |
| `--anthropic-api-key-file PATH` | `anthropic_api_key_file` |
| `--ignore-quota` | `ignore_quota`; use with an explicit subscription agent |
| `--auto-refresh` | `auto_refresh` |
| `--roadmap-only AREA` | `roadmap_only` |
| `--roadmap-skip AREAS` | `roadmap_skip`, as a comma-separated list |
| `--roadmap-targets FILE` | `roadmap_targets`, made absolute |
| `--extra-roadmap-prompt TEXT` | `extra_roadmap_prompt` |
| `--source PATH_OR_URL` | `source`; also requires `roadmap` in `--only` and a non-empty `--roadmap-only` |
| `--author-model MODEL` | `author_model` |
| `--author-effort EFFORT` | `author_effort`; Codex, Claude, or Kiro only |
| `--pace CURVE` | `pace` |
| `--pace-claude CURVE`, `--pace-codex CURVE` | `pace_claude`, `pace_codex` |
| `--stream` | `stream` |
| `--isolate-home` | `isolate_home`; useful when the id is `default` |

`add` cannot set `roadmap_extra_identities`, `respect_claims`, or `restart`, and
always writes `enabled = true`. Use `workers edit` for those.

## Actions

| Action | What it does |
| --- | --- |
| _(none)_ | Same as `status` |
| `status [--json] [--watch]` | Desired and actual state. Exits nonzero if the manager is offline or a wanted worker is not alive |
| `apply [--check]` | Validate the TOML schema and manager-level rules, then reconcile. `--check` validates only |
| `add [ID] [flags]` | Append an enabled definition and reconcile |
| `enable ID` / `disable ID` | Persist desired running or stopped state |
| `restart ID [--timeout SECONDS] [--no-wait]` | Restart on the current runtime once the round in flight finishes (at once between rounds), without changing desired state; a disabled worker stays stopped. Waits for the new instance unless `--no-wait`. See [Restarts and rolling deploys](#restarts-and-rolling-deploys) |
| `remove ID` | Drop the definition and stop the worker |
| `logs ID [--follow] [--lines N]` | The durable console log. `--follow` continues across worker restarts |
| `tmux [--no-attach]` | Build the optional tmux viewing workspace |
| `manager [--interval N]` | Run the reconciler in the foreground |
| `manager-stop [--leave-workers]` | Stop the detached manager. By default it also stops its workers |
| `service ACTION` | `install`, `uninstall`, `start`, `stop`, `restart`, or `status` for the native user service |
| `edit [--editor CMD]` | Create or open `workers.toml` in an editor; it does not apply the result |
| `import LEGACY [--force]` | One-shot migration from the legacy `workers.conf` format; it does not start a manager |

`apply` without `--check`, `add`, `enable`, `disable`, `remove`, and `restart`
start a manager if none is running and return only after it accepts control
requests. `edit`, `import`, and `apply --check` do not start one.

## Restarts and rolling deploys

A round is all-or-nothing: anything it has not pushed when it is killed is
discarded. Restarts therefore happen only between rounds. `tauceti workers
restart ID` asks the worker to finish the round it is running and then exit;
the manager starts it again on whatever code is installed now. The request
reaches the loop as `SIGUSR1` from its managed wrapper. A worker that is
between rounds (sleeping after a round, backing off, or waiting for quota,
budget, GitHub or fleet capacity) exits at once instead of starting another
round. A changed definition in `workers.toml` restarts the same way.

By default the command waits as long as the round takes, then until the new
instance is running; `TAUCETI_ROUND_TIMEOUT` (6 hours) still bounds any round.
`workers status` shows `restart: after the current round` meanwhile. Two
options change this:

- `--timeout SECONDS` stops the worker if its round is still running after
  that long, discarding the round, exactly as restarts did before.
  `--timeout 0` stops it immediately.
- `--no-wait` returns once the manager has accepted the request. The restart
  still happens at the round boundary, and so does the deadline: the manager
  enforces it, not the command. Interrupting a waiting command likewise leaves
  the restart pending.

`disable`, `remove` and `manager-stop` still stop workers at once.

To deploy new worker code without losing work:

1. Update the code the manager runs: `git pull` in a source checkout, or
   install the new package or runtime.
2. Restart the manager so that it, and the runners it launches, use the new
   code. The workers keep running and are adopted:
   `tauceti workers manager-stop --leave-workers`. The native service starts
   the new manager within seconds; without it, `tauceti workers apply` starts
   one. A manager that predates graceful restarts would kill the round, so
   `workers restart` refuses to use one unless you pass `--timeout 0`.
3. Restart the workers. One at a time, each finishing its round first, keeps
   the rest of the fleet working throughout:

   ```bash
   for id in $(tauceti workers status --json | jq -r '.workers[] | select(.enabled) | .id'); do
     tauceti workers restart "$id"
   done
   ```

   Or restart them all at once, each still finishing its own round:

   ```bash
   for id in $(tauceti workers status --json | jq -r '.workers[] | select(.enabled) | .id'); do
     tauceti workers restart --no-wait "$id"
   done
   ```

A runner still executing the previous release does not understand the
graceful request; the manager then waits until that worker reports that it is
sleeping between rounds, and stops it then.

## The manager, and running past logout

`workers apply` and related actions start a detached manager owned by the current
login session. To move an existing fleet to a native user service that survives
logout and comes back after a reboot, leave its workers running while the service
takes over the manager socket:

```bash
tauceti workers manager-stop --leave-workers
tauceti workers service install
tauceti workers service status
```

Omit `manager-stop` when no detached manager is running. One runtime directory
can host only one manager and one active configuration at a time. A reconcile
action that names a different `--config` file reports the conflict instead of
switching it.

That is a systemd user service on Linux and NixOS, or a LaunchAgent on macOS.
On Linux the service runs in `tauceti-workers.slice` and each worker moves itself
into its own transient scope there, as Fleet workers do. Restarting the service (to
adopt a new Worker runtime, for example) then adopts the running workers instead of
stopping their rounds. A service installed before this change keeps its workers in the
service itself: rerun `tauceti workers service install` and restart the service once,
at a quiet moment, to move to scopes.

The installer also writes `tauceti-workers.slice`. Limits set there cover the manager
and every worker scope together, so set them on the slice rather than with
`systemctl set-property` on the service, which worker scopes escape:

```bash
tauceti workers service install --cpu-quota 3200% --memory-high 48G
```

`--cpu-quota`, `--memory-high` and `--memory-max` take systemd's `CPUQuota=`,
`MemoryHigh=` and `MemoryMax=` values. A later install without an option keeps the
installed value, and an empty value (`--memory-high ''`) removes it. The install
reloads systemd, which applies new limits to running workers without a restart.

Two platform caveats:

- On Linux systems that stop the user manager after the last logout,
  `loginctl enable-linger "$USER"` keeps user services running with no login
  session.
- A macOS LaunchAgent survives terminal and SSH-session loss, but starts only
  after a graphical login. Installation says so explicitly when no GUI login
  domain is active.

At startup the worker preserves an explicit `SSL_CERT_FILE` or discovers the
host's system CA bundle, including NixOS's `/etc/ssl/certs/ca-certificates.crt`,
so quota checks keep working when uv's standalone Python defaults to a different
OpenSSL trust store. A shell-provided Nix bundle carried into the service stays
a revalidated candidate rather than a pinned trust path, so garbage collection
cannot silently disable fallback discovery.

The reconciler and the worker control sockets are portable Unix code. No Linux
`/proc` interface is required.

## Shared GitHub state by default

Workers targeting `TauCetiProject/TauCeti` use
`https://github-state.taucetiproject.org` for surveys and mirrored GitHub reads
without URL flags, token files, or per-worker configuration. The service publishes
read-only data for that public repository; GitHub writes still use the worker's
own authenticated account. Installation credentials remain on the server.

Explicit CLI and worker settings override the project configuration and default.
Use `--github-state-url ""`, `github_state_url = ""`, or project
`services.github_state.mode = "off"` to select direct reads. Without a project
manifest, `TAUCETI_GITHUB_STATE_URL=` also disables the default; project bootstrap
sets service environment bindings from the manifest. Downstream
projects retain their configured service and explicit `mode = "off"` policy;
other repositories are never implicitly sent to the Tau Ceti mirror. Custom and
private services still require their scoped client-token file. If a token file
is explicitly configured but unreadable, the client reports the configuration
failure rather than silently switching identities.

Unavailable mirror coverage or transport retains the existing direct fallback.
Fresh dispatch checks and GitHub mutations keep their existing behavior.

## Worker ids and credential isolation

Every worker id namespaces its state, checkout, review store, logs, and Bubble
home. Workers coordinate through GitHub rather than sharing local mutable state,
so several workers can use one host without sharing a checkout. A maintenance
branch claim lives in the PR head repository, where the worker that can update
the branch can also maintain its lease; roadmap and progress claims use the
canonical repository by default. Review markers, claims, and compare-and-swap
push and PR helpers keep workers from overwriting one another when they select
the same target.

Every id other than `default` also enables credential isolation. On Linux and
other non-macOS hosts, the worker gets a private `$HOME` containing private
Claude and Codex credential copies. GitHub CLI and Git configuration remain
shared so the worker still acts as the operator's `gh` account. `--isolate-home`
applies the same isolation when the id is literally `default`.

The Lean build caches are handled outside that isolation, in two different
ways, because they are written differently.

Toolchains are shared outright: `$ELAN_HOME` points at the login user's
`~/.elan`, so one install serves every worker and your own checkouts. Installing
a toolchain takes a per-toolchain lock and lands by rename, so workers racing for
the same one is safe. Left per-worker, 22 installs covered 6 distinct toolchains.

Mathlib's `.ltar` cache is *not* shared as a download target. `lake exe cache
get` takes no lock, and in any checkout older than
https://github.com/leanprover-community/mathlib4/pull/42752 it writes fixed-name
temporary files, so two workers downloading into one directory can leave a
corrupt `.ltar` under a name every later run trusts — including yours. Each
worker therefore downloads into its own `$MATHLIB_CACHE_DIR` under its state
directory, and before each round it exchanges *finished* files with the pool by
hardlink: it promotes what it fetched last round and takes a link to everything
the pool has that it lacks. A hardlink is atomic and an existing name is never
replaced, so the pool only ever gains complete artifacts, costs no extra disk,
and a worker downloads only what nobody on the machine has. Left per-worker
entirely, half of one week's 10.2 GB of `.ltar` traffic was a file another worker
already had.

`$LAKE_CACHE_DIR` stays per-worker too. It normally sits under the toolchain
directory, and would otherwise follow `$ELAN_HOME` into the pool, but unlike an
install it is written throughout every build. That keeps the default
`LAKE_ARTIFACT_CACHE` store private to the worker that fills it. Once canonical
`main` moves to a different `lean-toolchain`, the next host checkout preparation
clears this private store while no agent is
running. Switching between PR branches does not count as a bump and therefore
does not churn the cache.

Setting any of the three yourself overrides this. `scripts/share-build-caches`
folds the private copies an already-running fleet accumulated into the pool
without re-downloading them.

On macOS, `$HOME` stays unchanged because both Claude Code and GitHub CLI use the
login Keychain. `tauceti` redirects `$CLAUDE_CONFIG_DIR` and `$CODEX_HOME`, which
isolates Codex, but host workers still share the login user's Claude account.
Bubble rounds copy that shared Claude credential into a private transient
directory without modifying the Keychain. See [the sandbox notes](sandbox.md)
for that handoff.

What an isolated worker inherits is the credential, not your configuration. Its
`.claude` directory gets a `settings.json` of its own, no `CLAUDE.md`, and only
the one `pi` skill the worker itself dispatches through, so a round behaves the
same whoever ran it and your personal instructions cannot contradict the task
prompt. Edit that generated `settings.json` in place to tune a worker; it is
written once and never overwritten. `TAUCETI_INHERIT_CLAUDE_CONFIG=1` restores
the previous behaviour of sharing your own `CLAUDE.md`, settings, and skills,
including on a worker already seeded the clean way; a `settings.json` you have
edited is kept rather than replaced.

## Cleanup and uninstalling

Every round starts the cleanup collector (`tauceti cleanup --all-data-roots`) in
the background, at most once an hour per data root, at low CPU and I/O priority
and for at most 15 minutes, unless the systemd timer installed by `workers
service install` already collects that data root. The collector expires logs
and verification runs older than three days, idle checkouts, and shared build
pools, and deletes the data roots of earlier installations once no worker or
round holds their locks. It also deletes, for every worker and at every round
start, the recovery records, retained checkouts, cleanup queues, verification
checkpoints and continuation state that older releases kept: rounds are
all-or-nothing, so this legacy data is deleted, never migrated.
Its output goes to `cleanup-fallback.log` in the activity directory. Set
`TAUCETI_BACKGROUND_CLEANUP=off` to stop rounds from starting it.

To remove everything TauCetiWorker stored for your account, stop the workers and
run `tauceti purge` before uninstalling the code:

```bash
tauceti workers manager-stop
tauceti purge          # prints what would be removed
tauceti purge --yes    # removes it, and the worker service and timers
uv tool uninstall tauceti
```

`--config` also removes `workers.toml` and other settings. `~/.elan` is kept
because other Lean projects share it; `elan toolchain gc` reclaims toolchains
nothing uses.

## State on disk

| Path | Contents |
| --- | --- |
| `<state>/<id>.json` | Per-worker status, heartbeated every two seconds |
| `<state>/manager.log` | The detached manager's own console |
| `<state>/logs/<id>/work-*.log` | Durable per-run console logs |
| `<runtime>/manager.sock`, `w-<id>.sock` | Control sockets, mode 0600 |

`<state>` is `$TAUCETI_WORKERS_STATE_DIR`, else `$XDG_STATE_HOME/tauceti/workers`,
else `~/Library/Application Support/tauceti/state/workers` on macOS, else
`~/.local/state/tauceti/workers`. `<runtime>` is `$TAUCETI_RUNTIME_DIR`, else
`$XDG_RUNTIME_DIR/tauceti`, else `/tmp/tauceti-$(id -u)`; it must be owned by you
and inaccessible to other users, or the manager refuses to start.

Each managed worker publishes a structured state such as `waiting-quota`,
`surveying`, `running`, or `backoff`, along with its current phase and target and
the path to its logfile. That is what `tauceti workers status` and the
dashboard's workers view read.

Work rounds read kernel resource counters at start and finish, with live refreshes once a
minute. Telemetry never scans the host process table. Live statistics appear under
`resources` in `workers status --json`; completed rounds append structured records to
`logs/<worker-id>/resources.jsonl`. Cumulative completed-round statistics live in
`state/<worker-id>/resources.json` and survive restarts.

Completed-round CPU includes the round and its reaped descendants, using `getrusage()`.
A live value excludes unreaped children and has `cpu_complete=false`. Average CPU uses
elapsed wall time: 100% means one logical core. The worker average includes time between
rounds since recording began; the round average covers just that round.
`max_process_rss_bytes` is the kernel's single-process high-water reading, including
reaped children. New records do not report sampled process-tree RSS.

`resources.worker_scope` separately reports cumulative CPU, anonymous/file/kernel/charged
memory, scope-lifetime peak charged memory, task count (including threads), and optional
per-device I/O and stall counters. This scope includes the wrapper, loop, coding rounds,
and independent background verification. Its CPU must not be added to round CPU totals.
Charged memory and its peak include cache and kernel memory; they are not RSS, PSS, or an
exclusive share of shared Mathlib files. Missing counters remain unavailable, and workers
without a dedicated Linux scope retain round CPU accounting without scope measurements.
All cgroup access is read-only; unavailable telemetry does not delay work.

Reporting does not control whether useful work may continue. Status lock waits
are bounded and unsent fields retry on a later update; a failed status write leaves the supervisor's identity available
over its control socket and retries the pending fields on a later heartbeat.
Unavailable log files fall back to console output. Optional GitHub identity caches,
CLI instrumentation and endpoint metrics may also fail without rejecting an
authenticated request. A temporary CLI launcher preserves quota handling when
the normal launcher directory is unavailable, without moving retained publications.
Under these failures, retained metrics can be incomplete and worker status marks
degraded instrumentation.

A final cost-receipt write failure preserves the native agent's exit status and
result. The accounting bridge still attempts settlement, while missing receipts
retain their spending reservations until recovery. Actual funding admission and
provider quota handling continue to apply independently of reporting.

## tmux is a viewer, not the supervisor

`tauceti workers tmux` opens one dashboard window plus one log-following window
per enabled worker. Killing that session does not stop any worker, and running
the command again rebuilds the view from the configuration and the durable logs.
tmux is optional; workers run without it.

## Migrating from `workers.conf`

`workers.conf` is a legacy line-oriented format: one `tauceti work --loop`
command per line, each with an explicit `--worker-id`. It is only ever read by
the one-shot import, which refuses to overwrite an existing `workers.toml`
without `--force`:

```bash
tauceti workers import workers.conf
```

Anything the legacy parser does not recognize is an error naming the file, line,
and token, so a partial migration is never silently accepted.

## Integration-only dependency repairs

Bors retries an unchanged automatically reviewed bump after main advances, with
three retries per head. This uses the existing Bors heartbeat and leaves the
branch and its reviews intact. Workers do not request queue retries.

Bors durably hands a confirmed singleton failure to proof repair when no retry
is currently possible or its budget is exhausted, then publishes
`bump-integration`. Later main advances cannot retry that handed-off head.
The bump phase recognizes that status alongside a green standalone build. Before
spending on an editor, it checks the live exact-head PR and Bors `bump_repairs`
handoff. Pending or active queue work, withdrawals, draft/closed PRs and holds
are excluded. A second check after taking the normal branch claim closes the
selection gap. Failed observations decline the candidate and leave unrelated
work available. Automatic dependency bumps continue under the existing loop backoff and quota limits. The repair session fetches
and merges current main, diagnoses the actual integration failure, preserves
the dependency target and verifies its changes before pushing. Healthy surveys
make no additional API calls.

Claim collisions skip to other candidates without consuming a repair attempt. Only consecutive all-busy passes
use a small capped retry pause; they never enter the general failed-round backoff.

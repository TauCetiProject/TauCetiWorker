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
Linux workers also [wait for spare RAM and CPU before launching Lean](docs/lean-pressure.md).

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
| **Fix** | Address current-head review findings on one of our PRs: fix the code, or contest a wrong finding on its thread. Existing findings can be repaired while CI is pending or unavailable; a green build is not required. |
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

An admin's worker can combine the audit with any other phases, for example
`--only security-audit,bump,progress,overlap-scan`. Each round runs one bounded
quantum of the same scheduler as the parallel pool below, with that round's reviewer:
it advances passed ranges, inspects at most one range, and returns. New work is
admitted when at least 20 commits are pending; below that threshold the round continues
with its other phases without launching an audit model. Admitted ranges, including the
halves of a split range, continue below the threshold. Set
`TAUCETI_SECURITY_AUDIT_MIN_COMMITS` to a positive integer to change the threshold.
Audit-only runs inspect any nonempty backlog. While a range waits (its retry backoff, or a
binding range at the cursor), the round yields at once. Findings and unreadable evidence
stop audit advancement and preserve their blocks and evidence. The combined worker continues with its other phases; this
never clears a block or bypasses release gating. `--pr` cannot be combined with
the audit phase. Its isolated Luna/Haiku reviewer is unchanged, and the worker's
other phases use their default authoring profile.

It advances TauCeti's protected `security-audit` branch through main's first-parent
history, inspecting only `TauCeti/` and `TauCeti.lean` for IO and executable meta code.
Human-reviewed scripts, workflows, configuration and dependency pins are excluded.
Each session inspects complete per-commit diffs and before/after file contents,
up to 20 commits with a 2 MiB workload target. A single larger commit is inspected
alone without an aggregate byte cap, through the same compacting session. Each
source blob remains limited to 4 MiB, and each model launch to 30 minutes.
The batching target is separate from the model's live context window. Small
inputs are supplied inline; larger inputs use one read-only `audit.read_page` tool.
It accepts only a numbered chunk, with no paths, commands or copied
identifiers. The reader serves the next unserved chunk or an earlier reread and
rejects skipping ahead. The complete encoded tool response is capped at 64 KiB,
with headroom under the configured 120k output allowance. It validates size and
flushes the response before atomically recording served coverage. Failed writes
receive no credit; a crash before the receipt is committed can require a reread.
The Worker saves coverage before each client call and reconciles it with the
client's successful tool-call reports afterwards, including on timeout or failure.
For Codex, each successful read must also have its matching complete source
sections in the saved native conversation that a continuation resumes. The Worker
keeps the contiguous confirmed prefix and rereads only the unconfirmed remainder in the
same conversation. This works independently of parallel tool-call ordering.
If the Worker dies without saving a transcript, the launch's new reads are replayed.
Earlier findings and uncertainty are preserved;
partial progress never substitutes for a final safety judgment.

New inspections use judgment protocol 5 (the same served reader as protocol 4). The
reviewer returns exactly `pass`, `finding` or `unfinished`; there is no `incomplete`.
Genuine uncertainty about whether a construct is safe, or where it comes from, is a
`finding`: a concern for human review, not necessarily a demonstrated vulnerability.
Unread pages, lost context or running out of time are never findings: the reviewer
returns `unfinished` with no findings. A fresh attempt continues its own session while
it has no judgment, within one budget: three charged launches, where a launch that
served new pages is not charged; a timeout ends it. Native goal tools are not offered.
Codex registers its MCP resource helpers (`list_mcp_resources`,
`list_mcp_resource_templates`, `read_mcp_resource`) whenever any MCP server is
configured, and nothing turns them off: the model catalog describes them as unavailable,
and calls to them are inert (the only server, the audit reader, serves no resources, so
they read nothing and count as no coverage). New inspections never fall back to
protocol 3 or 4: until a client is verified for protocol 5 they wait, and the cursor
watchdog reports a long stall. The delivery check also confirms that Codex receives the
helpers' unavailable descriptions. Evidence retained from protocols 1 to 4 is only
classified; it is never continued.

New inspections run only after the installed client passes an intact
response check for this Worker release. The Worker runs this check automatically
when verification is missing or stale, serializes concurrent checks, and caches
failures for five minutes before retrying. A changed client bypasses that failure
cooldown. The selected protocol and verification failures are logged, with failure
details retained in `client-coverage-failures.json`. The same check is available
manually for diagnosis:

```bash
python3 -m tauceti_worker.audit_client_check --provider codex --state /path/to/worker/state/security-audit
python3 -m tauceti_worker.audit_client_check --provider claude --state /path/to/worker/state/security-audit
```

This runs the real client and reader against a loopback test endpoint, with
synthetic source and no subscription credentials or real model requests. It
compares the complete chunks in actual reviewer requests and, for Codex, in the
saved native conversation, including Unicode and escaped text. It writes a private
`client-coverage.json` compatibility record.
The record is tied to client paths, file hashes, version and the reader, launcher,
catalog and verification code. A client or Worker upgrade requires verification
again, which the Worker performs automatically. A failed verification makes new inspections
wait until a later check succeeds. Verification supports native executables and the
managed Codex launcher; other wrappers cannot be identified reliably, so inspections
with them wait.

Luna/Haiku receives no GitHub credential or general command/edit tools; trusted
Worker code alone verifies ancestry and moves the ref. Host-owned identity and
served coverage do not establish source safety.
No Lean builds or candidate scripts run. A finding, unreadable evidence or an
unsupported source blob stops advancement; an attempt with no judgment is retried. Read the retained `security-audit/blocked.json`
in that worker's state directory. Resolve findings by explicit human attestation,
not by rerunning the model until it passes. Do not change worker IDs to bypass a block.
The input, actual launcher/environment/prompt, transcript, native Codex
compaction/usage records, read receipt (for larger inputs), and durable ledger are
retained in the same directory.

For private Zulip alerts, set `TAUCETI_SECURITY_AUDIT_ZULIP_CONFIG` in the trusted
worker's environment to an absolute path to a mode-0600 JSON file:

```json
{"site":"https://leanprover.zulipchat.com","email":"BOT_EMAIL","api_key":"BOT_API_KEY","recipient_id":123}
```

Use a bot credential and the recipient's numeric Zulip user ID. The credential
and its configuration path are excluded from the isolated reviewer's environment.
Alerts include the commit range, recorded findings and local evidence paths.
Source findings and explained code-safety uncertainty send an immediate DM marked
**Human review required**. Operational pauses (unfinished turns, provider errors,
held operational blocks) send nothing themselves; the cursor watchdog below reports any
cursor held for an hour, whatever the cause, so one pause never produces two DMs.
Alerts are sent only as direct messages; no GitHub issue is created. A durable local
receipt suppresses repeated alerts for the same blocked range and kind. Failed
delivery is retried on a subsequent audit round or pool pass after at least 15 minutes,
checking the latest 100 messages in that private conversation for an earlier ambiguous delivery.
Notification failure preserves the audit block and allows bump/progress work to continue.

With alerts configured, one cursor watchdog guarantees that the cursor is advancing or the
operator has a DM. Every audit round (including maintenance turns and failed audits), every
pool pass and the standalone command below record the cursor in `security-audit/stall.json`.
Once audit work is due at one cursor (at least the configured threshold pending, or work
already started there: a held block or a pool range starting there) and that cursor has
not moved for an hour, an **Operational stall** DM is sent, then again daily while it stays held. An unobservable cursor (GitHub or
git failures) counts as held, and the DM names the error. While a pool holds the state, it
alone runs the watchdog. Checks and deliveries are serialized by local locks, so concurrent
callers never send the same DM twice. Run the command from a timer to cover a Worker or pool
that is dead or busy for longer than an hour; it uses the admission threshold its owner last
recorded:

```bash
python3 -m tauceti_worker.security_audit watchdog --state /path/to/worker/state/security-audit
```

Without this setting, reporting remains local to the worker's evidence and logs.

The reader rejects skipping unserved chunks and reports `next_unserved_page` and
`remaining_pages`. Served coverage drives continuation and splitting, never approval.

Every attempt, however it ends, gets one shared classification of its evidence
(`audit_evidence.classify`). It reads the retained session, every captured client
output (including archived earlier launches), the attempts it continued and the
ledger's verdicts for the same input. A concern recorded anywhere is a **finding**
and binds, even when the final answer is `unfinished` or `pass`, or the client timed
out or failed: a concern is a judgment's nonempty findings, or a `finding` label. An
`incomplete` label with no findings names no concern. Otherwise, evidence that
cannot be read (a row cut off where an answer could have been, or text mentioning a
judgment that cannot be parsed, or an answer that is not a judgment) holds the range
for a human like a finding, with an immediate **Unreadable evidence** DM. Judgments
are found by decoding JSON, including JSON carried in strings, never by matching text.
Each block records its classified outcome, and alerts read it from there. Otherwise
the attempt is a **pass** only when its last launch ended normally with a valid
complete pass and only the inspection's tools were used. Everything else
(unfinished, malformed output, a provider failure, an unexpected tool) is **not a
judgment** and never binds.

This phase can run alone or alongside bump/progress maintenance. It cannot use `--pr` or authoring-model overrides,
and is never enabled by `--skip` alone. `--agent codex` uses `gpt-6-luna`;
`--agent claude` uses Haiku, with ordinary subscription pacing and no expensive fallback.
To bill Haiku reviews to the Claude API instead, set
`TAUCETI_SECURITY_AUDIT_ANTHROPIC_API_KEY_FILE` to an absolute path to a mode-0600 file
containing only the key. Only the isolated client process receives it, and retained
evidence records a placeholder instead of the key.
It uses its own isolated launcher regardless of the ordinary host/Bubble setting.
The active GitHub credential must have repository-admin permission, checked before
spending and again before updating the cursor. New toolchain tags must be at or
behind the cursor; see [the policy](https://github.com/TauCetiProject/TauCeti/blob/main/docs/security-audit.md).

To catch up a large backlog, an admin can run several isolated reviewers at once
on the same audit state:

```bash
python3 -m tauceti_worker.security_audit pool --state /path/to/worker/state/security-audit \
  --parallel 8
```

On the queue host, `services/queue/install-security-audit.sh` runs the pool as
`tauceti-queue-security-audit.service` from its own origin/main release, like the other queue
units (`docs/queue/operations.md`, "Install and update"): `--parallel 16 --ignore-quota` on
`~/TauCetiQueue-data/security-audit-pool/state/pool/security-audit` (worker `pool` under the data root
`~/TauCetiQueue-data/security-audit-pool`), with alerts configured by
`~/.config/tauceti-queue/security-audit-zulip.json`, cursor updates use the security-audit GitHub
App in `~/.config/tauceti-security-audit`, and Haiku reviews fall back to the API key in
`~/.config/tauceti-queue/security-audit-anthropic-api-key` when neither subscription is available.

The pool plans a window of consecutive batches ahead of the cursor, each exactly as
`work` would plan it from that batch's start, and inspects them concurrently in
separate attempt directories. The cursor advances only through the ordinary admin
update, one batch at a time and only past validated passes, so a later batch that
finishes first waits for its predecessors. Each attempt's outcome is the shared
classification above. A finding or unreadable evidence is binding: the batch is never
inspected again, nothing beyond it advances, and the usual block and alert are
recorded. An attempt that is not a judgment never binds; it is retried autonomously by
bisection. If the reviewer engaged with the range (it answered or was served input) and
the range has several commits, the range is replaced, in one durable update, by its two
halves: consecutive slices of its exact commit list, the second starting at the first's
last commit, each a complete fresh inspection with full per-commit input. Pages or
partial approvals from different attempts are never combined. A single commit, or a
range whose reviewer never engaged (a provider or setup failure), is inspected afresh,
preferring the other reviewer when one is available, with a persisted backoff of up to
an hour that never gives up; the cursor watchdog reports a long stall. A recorded concern
about any commit binds every range containing it, however the commits are later split
or planned. A block left at the cursor by an ordinary round whose evidence binds nothing
is archived and inspected afresh; source findings never are.
The pool's whole scheduling state is one ordered list of active ranges,
`pool/ranges.json`, replaced atomically on every change: each range's exact commits,
status, verdict or block, attempts, reviewer, retry count and next retry time. Every
batch input is built by one function (`build_batch`) from a range's exact consecutive
first-parent commits, with complete per-commit images and diffs. After a restart the
list is restored while it is still the next main prefix; a pass is reused only when its
rebuilt input is byte-identical, and a block stays binding. When a human attestation moves
the cursor, ranges after it are kept, and a range it entered keeps only its remaining
commits: still binding if blocked or in flight, otherwise inspected afresh. A range that was running
when the pool stopped is first classified from its attempt's own evidence: a recorded
concern binds, and only an attempt with no judgment is retried. An unreadable list
holds the pool rather than replanning over a block. While the pool
holds `pool.lock`, ordinary audit rounds for that state defer to it. A GitHub rate
limit defers admin checks and cursor updates until reset without failing reviews.

Each new batch gets the first available reviewer: Codex `gpt-6-luna` while the Codex
subscription is under pace, then Haiku on the Claude subscription while that is under
pace, then Haiku billed to the API key in `TAUCETI_SECURITY_AUDIT_ANTHROPIC_API_KEY_FILE`
regardless of pace. With none available, new batches wait. Pacing is the ordinary
`work` verdict, and `--ignore-quota` overrides soft pacing only. Within one attempt,
Codex continues its native rollout and Haiku its Claude session (`claude -p --resume`),
which the Worker retains under the attempt's `claude-projects/`. Every retry is a fresh
attempt, and only an attempt classified as no judgment is retried, so a retry never
discards a concern. `--agent codex` or
`--agent claude` pins one provider. The ledger and `pool/` result files record each
review's provider, model and credential kind, never the key.

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
applies. Any job on an automatic Mathlib bump PR comes first, whatever its kind. `--only <task>[,<task>...]` pins it to particular kinds, and `--skip`
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
- `--extra-roadmap-prompt <text>` adds operator guidance to the roadmap prompt, for
  example `--extra-roadmap-prompt "Work on the material relevant to Higher Topos Theory"`.
  It steers the choice of target within the designated roadmap, and the choice
  of area too when `--roadmap-only ""` leaves that to the agent (an unpinned
  worker still draws its area at random before the agent sees the guidance).
  It cannot override the skip list, others' claims, or the repository's rules.
  Fix, rebase, and review rounds ignore it.
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

Code-writing rounds in Bubble are currently refused: the container would unpack
its own Mathlib build and build TauCeti privately, and a host keeps one shared copy
of each. Review rounds still run in Bubble, against a shared read-only Mathlib source.

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
that changed, and backs off repeated failures. Restarts wait for the worker's
current round to finish; see [rolling deploys](docs/workers.md#restarts-and-rolling-deploys).

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

Rounds clean up after themselves in every mode, starting a low-priority
collector at most hourly. To remove all TauCetiWorker data before uninstalling,
run `tauceti workers manager-stop`, then `tauceti purge --yes`, then
`uv tool uninstall tauceti`; see [cleanup and uninstalling](docs/workers.md#cleanup-and-uninstalling).

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
| `--pace-codex 0:0,100:100` | Give one provider's windows a curve of their own (`--pace-claude` likewise); the other keeps `--pace`. For an `auto` worker whose two subscriptions should be spent at different rates |
| `--ignore-quota` | Ignore pacing and transient usage-telemetry outages for an explicit `--agent codex` or `--agent claude`; known hard limits still apply |
| `--quota-cmd CMD` | Your own pacer, run as `<cmd> <agent>`: the first stdout token is the model to run; empty output means wait |

`TAUCETI_PACE`, `TAUCETI_PACE_CLAUDE`, `TAUCETI_PACE_CODEX` and
`TAUCETI_QUOTA_CMD` set the corresponding controls by default. Each pacing flag
overrides only its matching variable: an inherited provider curve still beats
`--pace`. Use `--pace-claude=` or `--pace-codex=` to return that provider to the
global curve. Status displays the effective curves. After a Claude
window resets, `tauceti` may make one small request to start its usage clock,
but only after it has found work and confirmed the other window has room.
[The quota notes](docs/quota.md) cover credential sources, the macOS Keychain,
and that bootstrap in detail.

## Claude API grants

Use explicit API billing with `--claude-billing api` and `--agent claude`.
API mode rejects subscription pacing settings, including inherited `TAUCETI_PACE*`
variables; unset them or pass the matching blank pacing flags to clear them.
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
An interrupted final ledger record is discarded automatically under the shared
lock; preserving its fragment is best effort. Ledger read/write failures do not
stop worker progress: work continues with incomplete accounting until storage
recovers. Funding and provider cooldown decisions still apply when readable.
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
provider session waits while a previous resume is still running; unrelated sessions
can run. Recovering the cumulative receipt restores accurate accounting across
resumes; a missing baseline does not prevent runtime work. A cost-only manual
reconciliation does not restore that baseline. No local budget amount is passed as
Claude's `--max-budget-usd`. After a crashed worker, first try
`tauceti budget --recover --note "worker crash"` to recover its durable receipts
and remove stale staged keys. Missing native totals can settle as labelled,
correctable estimates rather than remaining unresolved indefinitely.

Fleet spending also shows rates including live estimates. The existing manager
refreshes these once per minute, independently of historical activity collection.
It uses a small running-usage summary and, when calibrated, journal-size growth;
sampling never parses the growing journal. Recorded spending remains available
alongside the labelled estimate, and reservations stay excluded. Resumed sessions
retain their attributed known prefix until deduplication supplies further usage.
The detached usage supervisor applies the audited stopped-usage policy to eligible
stopped invocations on ordinary minute passes; late native receipts correct those
estimates. Reporting estimates do not change admissions or settle expenditure.

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

The `overlap-scan` and `overlap-fix` phases maintain a shared Mathlib cursor and
adopt overlapping upstream APIs. See [Mathlib overlap maintenance](docs/mathlib-overlaps.md)
for initialization, coordination, and the live calibration results.

Run Python host helpers from this checkout with `uv run python PATH_TO_HELPER` so they
use the pinned shared Tools dependency. Update any local timer commands to use the same
invocation before upgrading this checkout.

GitHub transport state (shared cache, cooldowns and pending publications) lives in
`$XDG_STATE_HOME/tauceti/github-shared` or `~/.local/state/tauceti/github-shared`,
independently of the installed worker version. `TAUCETI_GITHUB_SHARED_DIR` overrides
this location. Comment writes are spaced across workers sharing a GitHub identity;
secondary throttling backs off without discarding completed agent output.

Plain `gh pr view --repo OWNER/REPO --json ...` scalar metadata reads also use
the configured shared state service. Unsupported fields, formatting options,
unavailable service data, and explicit direct-read contexts retain the normal
CLI path. Exact-head review claims are checked as a read-only hint before
preflight API reads; the engine still acquires and validates its existing claim.
Request diagnostics include JSON field names and distinguish closed-PR history
scans from open inventories. Progress batches complete labelled histories with
independent cursors rather than caching a potentially incomplete reporting window.

Use `tauceti work --self-tested` (or `self_tested = true` in a worker definition) to instruct source-changing agents to apply the `self-tested` label to eligible mathematics PRs after successful local verification. The existing shared checks cover `lake build --iofail && lake test && lake lint`; no additional verification pass is needed. Use `--no-self-tested` to disable the prompting. If direct labelling lacks permission, agents can request the label with a new standalone `self-tested` PR comment; only the PR author can use that command, and it declares that the author runs local verification. The label persists through fix/rebase pushes; remove it if verification fails or is incomplete. Members of `TauCetiProject/self-tested` have triage access after accepting their organisation invitation. Reviews and staging tests remain required.

Agents run three of the PR template's quality-pass checks by default, Q1, Q2 and Q5, and begin each other line `declined — not run`. Use `tauceti work --full-quality` (or `full_quality = true` in a worker definition) to have them run all five.

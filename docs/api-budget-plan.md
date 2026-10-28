# Claude API billing and local grants

Implementation design for the supported Claude API grant interface.

Explicit Claude API billing uses one durable budget shared by participating
workers on the same machine. Grants authorize starting sessions. A session that
has started always runs to completion as far as the local budget is concerned,
even if its cost exceeds the available balance. Automatic grants replenish the
balance over time, including while workers are stopped.

## Command interface

| Command or flag | Meaning |
| --- | --- |
| `tauceti budget` | Show balance, rate, pending costs, active sessions, waiting workers, and ledger path. |
| `tauceti budget --grant 100` | Add $100 to the balance. Repeating it adds another $100. |
| `tauceti budget --set-grant 100` | Set the current balance to $100 through a logged adjustment. |
| `tauceti budget --grant-rate 20` | Set automatic replenishment to $20/hour from now onward. |
| `tauceti budget --grant-rate 0` | Stop automatic replenishment after accounting for credits earned so far. |
| `tauceti budget --json` | Return the same state in a stable machine-readable format. |
| `tauceti budget --log` | Show the append-only event history; combine with `--json` for JSONL. |
| `tauceti work --agent claude --claude-billing api --budget` | Run with API credentials and admission through the shared local budget. |
| `--anthropic-api-key-file PATH` | Read the API key from a private file, instead of `ANTHROPIC_API_KEY`. |

`--grant` and `--set-grant` are mutually exclusive. Either may be combined with
`--grant-rate` in one atomic operation. Grants must be positive, rates
nonnegative, and set balances may be negative to represent a deficit. Reject
NaN, infinity, and malformed values. USD is the only currency in this version.

Budget mutations belong to `budget`, not worker definitions or loop startup:
restarting a worker must never add a grant again. `--set-grant` first accrues
credits at the previous rate, then records the previous balance, requested
balance, and adjustment. It changes neither historical spending nor the rate.
Unsettled session costs remain payable after the adjustment.

API billing requires an explicit `--agent claude` in the first version. Reject
subscription-only quota overrides in this mode rather than silently combining
two governors. `--budget` requires API mode in this version. Existing
subscription behavior remains the default. Explicit API mode without `--budget`
is possible and is clearly reported as having no local grant governor.

## Example interaction

```console
$ tauceti budget --grant 100
Added $100.00. Balance: $100.00

$ tauceti budget --grant-rate 20
Automatic grant: $20.00/hour from now onward.

$ tauceti budget
Local budget                         10:00 UTC
  Balance                            $100.00
  Grant rate                          $20.00/hour
  Recorded session spend               $0.00
  Active sessions                     0
  Unresolved session costs            0
  New sessions                        allowed
  Ledger                              ~/.local/state/tauceti/budget/events.jsonl

$ tauceti work --agent claude --claude-billing api --budget --loop --worker-id worker1 &
worker1: using local budget; starting session

$ tauceti work --agent claude --claude-billing api --budget --loop --worker-id worker2 &
worker2: using local budget; starting session

# Thirty minutes later, both sessions have completed, costing $118 together.
$ tauceti budget
Local budget                         10:30 UTC
  Balance                             -$8.00
  Grant rate                          $20.00/hour
  Recorded session spend             $118.00
  Active sessions                     0
  Unresolved session costs            0
  New sessions                        waiting for funding
  Balance reaches zero                10:54 UTC
  Ledger                              ~/.local/state/tauceti/budget/events.jsonl

$ tauceti budget --set-grant 50
Set balance from -$8.00 to $50.00; recorded adjustment +$58.00.
Grant rate remains $20.00/hour. Waiting workers will recheck funding.
```

The example assumes API credentials are configured and admission estimates are
available from earlier Claude runs. Reaching zero does not itself authorize a
new session: the balance must also cover its admission estimate. Status should
show that separate threshold and its estimated wake time when a waiting task's
estimate is known.

## Shared state and append-only history

Use a user-level state directory, independent of checkout, package installation,
worker ID, and isolated agent home. Default to
`$XDG_STATE_HOME/tauceti/budget/`, or `~/.local/state/tauceti/budget/` on Linux;
use `~/Library/Application Support/tauceti/state/budget/` on macOS. Allow
`TAUCETI_BUDGET_DIR` to select another local directory. Resolve the absolute path
before home isolation and pass it through managers, round children, and Bubble
launches. All participants must resolve the same directory; different OS users
need an explicitly shared directory and suitable permissions.

`events.jsonl` is the authoritative append-only ledger. `budget.lock` serializes
transactions with `flock`. `projection.sqlite` is a disposable indexed projection: it
records the processed byte offset and sequence and replays only the appended tail.
Current sessions, recent cost samples and cumulative provider baselines are indexed;
routine admission and settlement do not load every historical session. A lost,
corrupt or replaced projection rebuilds automatically from the ledger. A client
using the older JSONL-only implementation may still append under the same lock;
the next reader incorporates its events. If projection storage is unavailable,
operations fall back to full authoritative replay.
Create private directories/files by default. No API key, prompt, or response
content belongs in this ledger. Complete history is retained; recovery may
discard an incomplete final append. An adjustment or correction appends a new event.

Each committed JSONL record contains a schema version, increasing sequence,
transaction ID, UTC timestamp, actor, and its ordered event batch. Event types
include initialization, accrued grant, added grant, balance set, rate changed,
session admitted, session settled, settlement corrected, and accounting
unresolved. Session events carry invocation ID, worker ID, phase, model,
provider session ID when available, estimate/cost, and outcome. Settlements
have idempotency keys so reprocessing cannot debit the same invocation twice.

Under the lock, catch the projection up with the ledger, accrue elapsed credits, validate the requested
operation against current state, append the complete transaction, and `fsync`
when storage is available. Ledger failures permit runtime work to continue with
incomplete accounting; bookkeeping durability is not a launch requirement.
An optional snapshot is written only
after the ledger commits and never overrides ledger history. Use decimal money
and retain fractional accrual internally; rounding for display must not change
accounting. Queries project accrual to the current instant without appending a
record merely because somebody checked the balance.

Rate changes accrue at the old rate up to the change and use the new rate
afterward. Idle time earns credits with no upper accumulation limit. Backward
clock movement earns no negative credits and cannot count an interval twice;
keep a persisted accrual watermark and report clock anomalies. A forward clock
change affects this wall-clock policy and should be visible in the history.

On every locked access, an interrupted final write is recovered automatically.
A complete JSON record missing only its newline is retained. Otherwise its
fragment is saved as `events.discarded-*.json` when possible and removed from
the ledger; archive failure never blocks recovery. A failed short append also
tries to remove the bytes it just wrote. No unresolved repair marker is created,
and legacy repair markers do not pause work or reserve funds.
`budget --repair-ledger --note TEXT` remains a compatibility command for this
automatic recovery. Complete invalid records and sequence gaps are reported by
accounting commands, while runtime admissions continue without usable ledger
state. A failed admission write likewise permits work; available funding and
cooldown decisions still apply when the ledger can be read. Process supervision
continues independently of ledger availability, and surviving usage receipts
remain available for later accounting recovery. An unrecorded admission is
retained in its existing lifecycle file. The bridge or supervisor records it
idempotently when the ledger becomes usable, then applies its native receipt.
This recovery records work that already ran and makes no new funding decision.
A corrupt ledger does not invalidate a healthy projection or trigger repeated
full replays; the supervisor resumes accounting when the ledger bytes change.

## Admission and concurrent sessions

Define the recorded balance as grants plus logged adjustments minus settled
session costs. Track active sessions separately, with estimated pending costs.
The amount available for another admission is the balance minus those estimates.
The check and the new session's estimate are committed in the same transaction,
so workers cannot all launch against the same uncommitted funds.

Estimates are scheduling hints, not allowances enforced against a running agent.
Learn them from completed sessions by model and phase, using a conservative
recent percentile. Zero-cost launch failures do not establish a spending estimate.
With no positive recorded settlements, a calibration session reserves at least
$1 or the largest of the last 20 recorded costs, whichever is higher. Admit one
when the available funds cover that reservation; other sessions for that model
wait while its calibration
session is active. If that session loses its receipt, admit a replacement instead
of waiting for reconciliation. Thereafter, multiple workers can run whenever the balance covers their
estimates. This avoids introducing a user-facing per-session cap.

On completion, replace the pending estimate with the reported cost in one
transaction. Underestimates may take the balance negative; overestimates release
room for another session. Local funding changes, including `--set-grant 0` and
`--grant-rate 0`, only affect subsequent admissions. Never pass
`--max-budget-usd`, cancel subagents, revoke credentials, or send a termination
signal in response to local budget state. Operator stops and existing unrelated
timeout behavior retain their existing meaning.

Check admission immediately before a concrete paid launch, after survey and
non-model preflight. Finding no work costs nothing. Funding waits happen in the
outer loop, outside the round timeout, with rechecks at most 60 seconds apart and
a projected funding time for the learned estimate when available. Stage preflight
uses the same learned estimate, or the worker's live queue estimate. The shim never waits for
funding inside a round. One-shot commands report the block without launching.
Show balance, active estimates, admission threshold, and waiting reason; avoid
describing estimates as already billed costs. Serialize waiting admissions and
use a queue for funding waits within each model. Waiting liveness lives in a
disposable state file; only changes of waiting reason go into the audit ledger.
Stale tickets expire after 120 seconds. Cooldowns on other models do not hold up
funded admissions. Local denials have a distinct round exit code and outer retries
that back off from five to at most 60 seconds, separately from no-progress backoff;
authentication and launch failures use normal failure backoff. Funding waits refresh
queue liveness while they recheck.

## API credentials and cost recording

Add billing mode to round options and managed worker specifications, and
propagate it into every child launch. Centralize Claude environment construction
in `agents.py`: preserve subscription key stripping in subscription mode, and
in API mode inject the chosen key while removing conflicting OAuth/provider
environment routing. Verify the effective authentication without a paid probe;
missing keys or conflicting managed settings fail before starting work. Keep
raw keys out of command arguments, worker config, status, and debug output.
Managed workers should use a key-file path; store that path rather than the key.

Bubble uses a private, read-only key handoff and loads the key inside the
container. It must not require subscription credentials in API mode. The host
owns the ledger and issues invocation IDs. The container has a read-write
requests/receipts mailbox and read-only replies/heartbeat mount, without access
to the host shim, host key, or ledger. Host reads reject symlinks and oversized
messages. Cost receipts remain self-reported: this is local accounting, not an
attestation against a deliberately dishonest container. Key staging is cleaned
up after the invocation. Raw keys are injected only into Claude and the external
review engine's API-authentication path, not unrelated host/build subprocesses.

Capture structured result data before transcript normalization. Persist a
minimal cost receipt for each invocation separately from readable narration;
record success and failure costs. Use the final cumulative `total_cost_usd`,
including Claude-managed subagents, once per fresh invocation. An explicit
`--resume SESSION_ID` uses the recorded provider session baseline when available;
otherwise cumulative costs are recorded from zero. `--continue` is refused
because it cannot identify the provider session. Resumed sessions subtract
already recorded totals; multiple cumulative results must
not be summed. Session resets require accounting for each segment separately.

Claude's cost figures are local estimates, not authoritative Console charges.
Missing or crash-zeroed results do not establish zero spending. Preserve the
receipt and mark the invocation unresolved. Retain its existing pending estimate
until the cost is recovered or an operator records a correction with an
explanation. Unresolved costs are audit work, not a fleet-wide admission pause;
funded workers continue, and orphaned sessions do not occupy calibration slots.
A running resumed invocation holds only that provider session's resumption
slot. Missing costs from a stopped invocation do not prevent further resumes.
Recovering its cumulative receipt improves accounting accuracy on subsequent
resumes; a cost-only manual reconciliation does not restore that baseline.
On Linux, recorded bridge process identities include boot and process start
markers. A live owner prevents orphan detection, receipt recovery or active
reconciliation based solely on a delayed heartbeat.
`budget --recover --note TEXT` reads durable receipts from stopped bridges and
removes their staged keys. It skips live bridges. `--reconcile` refuses a
running session with a live heartbeat. A native process that could not be
started settles at zero; a process that ran without a reliable result remains
unresolved.
Do not release a pending estimate merely because its process disappeared or a
timer expired. If logging fails during an active session, let it finish, retain
its cost receipt, and keep the original reservation pending reconciliation.
Other workers continue against the available balance. Corrupt ledger bytes and
an unaudited ledger-repair marker remain separate integrity blockers.
See Anthropic's [cost tracking documentation](https://code.claude.com/docs/en/agent-sdk/cost-tracking).

`tauceti-review` is a separate launch path. Its host and Bubble
Claude runners reach the same standalone shim through PATH. The shim keeps
its private configuration beside its executable so a clean reviewer environment
cannot lose the credentials or bridge. The engine runs with `--auth api`;
its literal `claude` calls reach the shim. The engine recognizes the shim's stable
`tauceti-local-admission:` marker and aborts before posting a scoreboard on local
denials. Every independent paid session, including parallel reviewers and
retries, needs its own invocation record. A session already admitted includes
its internal Claude subagents and tool loop. A new independent invocation needs
another admission. Budgeted review must fail before spending if the engine
cannot honor the protocol; do not ship author/fixer-only accounting as complete
coverage. Inspect Progress and other external runners for the same requirement.

## API throughput and observability

API mode bypasses subscription usage polling, OAuth refresh, and quota bootstrap.
It still respects actual API throttling. Keep throughput limits separate from
dollar funding: keys do not multiply organization capacity. Claude handles its
native request retries; observed 429 retry events produce shared model
cooldowns for new admissions. Cooldown recording does not block draining an
active session's output. Provider errors remain visible in the transcript;
local funding and provider throughput waits have separate reasons. The local budget never kills
a session; provider refusal can still prevent it from making another request.
See Anthropic's [API rate limits](https://platform.claude.com/docs/en/api/rate-limits).

Expose API billing, balance, pending estimates, unresolved accounting, session
counts, funding waits, throughput waits, and ledger location through `budget`,
runtime status, and the dashboard. Persist worker `budget`, `claude_billing`, and
`anthropic_api_key_file` fields through serialization, fingerprints, manager
restarts, and `_round` arguments. Funding edits do not restart workers or their
active sessions. Console balances remain separate telemetry: local grants are
a scheduling policy, not a transfer of Anthropic credits.

## Implementation map

1. `budget.py` owns ledger replay, decimal accrual, locked admissions, fair
   waiting tickets, settlements, corrections, and torn-write recovery.
   `budget_cli.py` exposes mutations, JSON status, and history.
2. `claude_api.py` resolves credentials before home isolation and starts a host
   bridge for each concrete work stage. `claude_api_client.py` is a standalone
   shim that verifies API authentication, requests admission, streams Claude
   output, and preserves minimal cost receipts.
3. Host and Bubble work, reviewer calls, and progress authoring use that shim.
   Bubble receives a private key mount and a bridge mailbox, without writable
   access to the ledger or subscription credentials.
4. CLI, loop children, and managed specifications propagate billing, budget, and
   key-file settings. Runtime status and the Workers dashboard show budget
   state. API retry events create shared model cooldowns.
5. `tests/api_budget.py` exercises the ledger and fake Claude subprocesses,
   including a simulated Bubble mount boundary. The existing suite verifies
   subscription behavior. Real provider throughput and live Bubble deployments
   should be checked with the intended grant account.

## Required verification

Use an injected clock and fake Claude/reviewer processes; no paid calls are
needed for these checks. Verify grants, balance setting, zero/negative balances,
rate changes, fractional accrual, idle time, and restart replay. Verify that a
set while sessions run changes the current balance but preserves future debits.

Exercise simultaneous admission and settlement in separate processes,
idempotent receipt replay, partial writes, and crashes before/after ledger commit
and process launch. Verify a full replay matches snapshots and that ledger
failures do not block runtime work. Verify fair waiting and pending
estimates prevent every worker from claiming the same money.

Use a fake long session whose cost exceeds its estimate and balance. Assert it
and its subagents finish, the balance goes negative, and later sessions wait.
Changing balance or rate during that session must never signal its process.
There must be no budget-triggered termination or Claude budget-cap argument.

Verify API credentials and cost receipts across host, Bubble, `_round`, manager,
reviewer, and external runner paths, including secrets redaction and cleanup.
Verify cumulative/subagent costs are counted once, failures can incur spending,
and missing costs remain visible for reconciliation without pausing funded
workers. Verify orphaned calibration sessions can be replaced, unresolved
estimates remain reserved, and ledger-repair markers do not block work.
Exercise provider cooldowns separately
from funding waits. Retain existing subscription behavior and its checks.

## Durable response usage and recovery

The native Claude shim records content-free usage in `budget/usage/<bridge>/<invocation>.json`
and an append-only delta journal. Evidence includes provider response/session IDs,
model, timestamps, token counters and a hash of the vendored dated price table.
It contains no prompts, response text or keys. Each usage change is synced before
forwarding the corresponding event; completed responses do not depend on a later
stdout event or final Claude result. Atomic checkpoints and the preceding valid
journal prefix survive process termination and torn writes. Bubble mounts only its own round
usage directory separately from the bridge and never receives the ledger.

New invocations also record their exact admission ticket, invocation ID, bridge,
shim and native process birth identities in `budget/lifecycles/`. The admission
ledger event includes the initial lifecycle identity, so a bridge crash before
writing its sidecar is recoverable. A detached host supervisor owns the sidecars
and recovery, outside both the round process group and any agent sandbox. It starts at API worker/context startup and is checked again at loop
round boundaries. Restarting it replays the local evidence automatically; it does
not poll providers or GitHub. Recovery progress is persisted, and a separate
heartbeat remains responsive during ledger recovery. Admission checks restart a
lost supervisor; source updates retire it at an invocation boundary. Supervisors
without ledger-independent processing are replaced automatically, while the existing
protocol remains compatible with older workers. Only registered new invocations
participate in this automatic recovery; historical cleanup remains
separate. Reconciled native receipts are archived under `lifecycles/done/`;
unchanged provisional journals are checked by file stamp without reparsing them.

Every native launch, including an explicit resume or copied Bubble shim, must
create a durable usage journal. A native exec gate waits until the supervisor has
verified the journal and recorded the native process identity, then receives a
durable permit containing a second copy of that native identity. A lost sidecar
can recover the permitted identity without reclassifying real inference as zero
spend. Missing or malformed accounting configuration fails before
inference. Linux identities include the boot ID, start ticks, PID namespace and
the observer's PID namespace;
namespace-local PIDs are translated while the gate is alive. macOS identities use
the boot session and process birth time in microseconds. On other platforms an
unknown identity cannot authorize automatic stopped-process reconciliation.
For Bubble across a VM or a host-uninspectable container, the bridge explicitly
records the remote boot/start/namespace identity without interpreting its PID on
the host. An exact gate-linked native exit receipt proves termination there;
bridge death without that receipt records `remote-liveness-unconfirmed` and
reserves observed usage plus an uncertain tail without charging a live invocation.
A successful host Bubble cleanup records the exact non-reusable boundary token
in its read-only outbox; cleanup for a prior boundary cannot stop a newer
invocation. This local proof permits prefix recovery, retaining an uncertain
tail until an authoritative receipt arrives. Receipts and journals remain
available for later local reconciliation.

Lifecycle evidence distinguishes observed native success, nonzero exit, signal,
exec failure, bridge death and parent death; the outer loop records round timeout
and signal outcomes separately. An unobserved death is recorded as stopped, not
assigned a guessed exit status. A stopped process or `inflight=false` never closes
a journal. Normal journal closure requires a successfully drained native exit, a
successful result event and healthy journal writes. Without that proof recovery retains the
priced prefix and an explicitly uncertain tail reserve. An admission with no
launch evidence, or a gate-linked no-exec receipt, proves no inference and permits
exact zero settlement.
Late authoritative receipts remain readable and replace estimates with existing
ledger corrections.

For rollout, write `record-only` to `budget/usage-recovery-mode` before deployment.
The supervisor records launches and exits while normal bridge receipts continue
to settle. Observe naturally selected rounds, then change the file to `auto` to
enable stopped-invocation recovery (the default when the file is absent). Workers
from another installation can share a supervisor with the same protocol. To hand
ownership to a new installation, call `ensure_supervisor(budget_dir(), refresh=True)`
from that installation's `tauceti_worker.usage_supervisor` and `tauceti_worker.budget`
modules; this retires the exact supervisor through a local marker and preserves
paid processes. Changes
are read on each pass and do not restart paid sessions. Rollback can restore
`record-only`; it must retain lifecycle evidence, permits, receipts,
journals and reservations; it must not undo committed corrections.

Response counters merge by ID, including partial/repeated events. Replayed
responses on resumed invocations are not charged twice. Decimal token pricing
honours cache-write TTLs. Unknown models, missing counters, unpriced server-tool/priority-tier usage and
requests still in flight remain incomplete rather than becoming zero-cost completed work.

`tauceti budget --recover --dry-run --json` prepares a content-free manifest under
`budget/recovery-audits/` without changing the ledger or deleting bridge keys.
Every invocation has its recorded owner identity, current owner/heartbeat check,
evidence source, known prefix, existing charge, reservation, proposed estimate,
final total, spend delta and released reservation recorded. The manifest includes
ledger sequence/hash, evidence hashes, interval endpoints, comparable IDs/costs/
durations, median rate and pricing-table hash when available. It contains no
prompts, response text, credentials or receipt outcome text.

Apply with `tauceti budget --recover --note 'stopped usage policy v1' --json`.
To bind application to the reviewed proposal, pass `--plan /path/to/dry-run.json`;
a changed ledger or proposal requires another dry run. Application produces a fresh
manifest and rechecks process identities immediately
before its single append-only ledger transaction. Inspect the dry run's identities,
evidence and totals first. Both a live process identity and a fresh bridge heartbeat
protect **all** session statuses from modification, including late cumulative
corrections that would affect a live successor. No grant or worker ceiling changes
are part of recovery.

Recovery tries authoritative native receipts first, then directly linked durable
usage journals. Legacy transcripts are considered only when a saved receipt names
the exact provider session and exactly one file belongs to that worker; worker/time
overlap never establishes identity. Recorded finish times and later admissions
resuming that exact provider session bound transcript attribution. Recovery shares
response IDs across its plan, so a resume cannot charge responses recovered earlier
in the same transaction. Complete saved usage can settle as a marked,
correctable token-price estimate. An unclosed journal, an interrupted response,
or a transcript remains incomplete even if all observed responses are priced,
the owner has died, or `inflight=false`.

For stopped incomplete usage (including sessions with no journal), policy
`stopped-usage-v1` uses the median native cost per second of at least three sessions
of the same model and phase completed in the seven days preceding admission.
With fewer than three, it uses at least three sessions of that model in the same
window. Estimates, manually reconciled totals, the invocation itself, negative
or zero costs, failed launches, missing/zero/invalid intervals and future endpoints
are excluded. The rate is multiplied by this invocation's recorded duration and
rounded to twelve decimal places in USD (half-even). Normal native settlement
timestamps or explicitly recorded finish times supply endpoints. The native shim records exit time in its final receipt when process exit
is confirmed, including interrupted exits; provisional in-flight checkpoints never
claim an exit. Old recovery timestamps without recorded endpoints are excluded.
If a target has no recorded exit, a usage observation time can supply its end.
That measures the observed portion rather than a confirmed process lifetime; the
duration source remains labelled in the audit. Orphan detection and repair time never supply an endpoint.
Replayed native receipts retain the original recorded endpoint; missing endpoints
exclude them from rate comparables. An unusable duration or insufficient
comparables uses the **saved prelaunch estimate**, without recalculating admission
estimates. The resulting total is at least the already charged/known priced prefix.
It never adds the prefix to the estimated total.

The policy settles this labelled total and releases its residual reservation.
The manifest reconciles before/after spend exactly to charge deltas and explains
reservation releases separately: releasing a reservation is neither spending nor
a refund. Repeat recovery makes no monetary change. A late native receipt replaces
an estimate through idempotent corrections, including resumed cumulative baselines.
During ordinary live teardown, `recover_usage` still charges known prefixes and
retains reservations when evidence is incomplete; only explicit stopped recovery
applies this policy.

Usage evidence is collected outside the fleet ledger lock; planning replays the
current ledger incrementally inside the lock and skips estimates with no new native
receipt. Full comparable details stay in the manifest; ledger events retain a compact
policy/rate/duration summary, count, audit hash and manifest reference.

Prepared activity caches distinguish live usage, settled estimates (including
recovered estimates), and unresolved stopped accounting. Existing spend and dollars
per LOC measurements remain available, including during mixed version 2/3 rollouts.
Estimates use their recorded interval in rate windows; without an interval they are
placed at admission instead of being spread across downtime until repair.
Reservations never enter spend reports;
`status` still reads prepared local data without GitHub calls or recovery analysis.

The authoritative ledger and retained recovery manifests are append-only evidence.
Ledger repair transactions name their manifest; a manifest records a plan, and the
matching ledger transaction establishes application. Manifests explicitly have
`status: planned`; a separate `.application.json` records the committed transaction
and sequence (or a no-change result). If a run stops before append,
rerun dry-run and recovery. Do not truncate evidence or recreate consumed reservations.
Correct a mistaken total with `tauceti budget --reconcile INVOCATION --cost USD
--note 'reason and supporting evidence'`; this appends a compensating adjustment.
Optional provider-admin reconciliation is separate work and does not block stopped
settlement. Recovery is independent of issue #313.

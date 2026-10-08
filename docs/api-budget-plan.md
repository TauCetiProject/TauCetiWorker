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
transactions with `flock`; a snapshot may accelerate replay but is disposable.
Create private directories/files by default. No API key, prompt, or response
content belongs in this ledger. Routine operations never rewrite, truncate,
rotate, or delete history. An adjustment or correction appends a new event.

Each committed JSONL record contains a schema version, increasing sequence,
transaction ID, UTC timestamp, actor, and its ordered event batch. Event types
include initialization, accrued grant, added grant, balance set, rate changed,
session admitted, session settled, settlement corrected, and accounting
unresolved. Session events carry invocation ID, worker ID, phase, model,
provider session ID when available, estimate/cost, and outcome. Settlements
have idempotency keys so reprocessing cannot debit the same invocation twice.

Under the lock, replay the ledger, accrue elapsed credits, validate the requested
operation against current state, append the complete transaction, and `fsync`
before allowing a paid process to start. An optional snapshot is written only
after the ledger commits and never overrides ledger history. Use decimal money
and retain fractional accrual internally; rounding for display must not change
accounting. Queries project accrual to the current instant without appending a
record merely because somebody checked the balance.

Rate changes accrue at the old rate up to the change and use the new rate
afterward. Idle time earns credits with no upper accumulation limit. Backward
clock movement earns no negative credits and cannot count an interval twice;
keep a persisted accrual watermark and report clock anomalies. A forward clock
change affects this wall-clock policy and should be visible in the history.

Malformed records, sequence gaps, or a torn final write block new admissions and
report the problem. For a torn final write, `budget --repair-ledger --note TEXT` archives the
original bytes, atomically replaces the ledger with the verified prefix plus an unresolved
repair entry naming the archive. Admissions remain blocked until that entry is
reconciled after investigation. Complete invalid records require manual
investigation; they are never silently skipped. A valid transaction whose acknowledgement
was lost is discovered by replay and its idempotency key.

## Admission and concurrent sessions

Define the recorded balance as grants plus logged adjustments minus settled
session costs. Track active sessions separately, with estimated pending costs.
The amount available for another admission is the balance minus those estimates.
The check and the new session's estimate are committed in the same transaction,
so workers cannot all launch against the same uncommitted funds.

Estimates are scheduling hints, not allowances enforced against a running agent.
Learn them from completed sessions by model and phase, using a conservative
recent percentile. With no usable recorded settlements, admit one calibration session for a model when the
balance is positive; other sessions for that model wait for its first cost
result. Thereafter, multiple workers can run whenever the balance covers their
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
funded admissions. Local denials have a distinct round exit code and a five-second
outer retry, without increasing the no-progress backoff; funding waits refresh
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
including Claude-managed subagents, once per fresh invocation. An explicit `--resume SESSION_ID` requires a recorded provider session
baseline; `--continue` is refused because it cannot identify that baseline before
launch. Resumed sessions subtract already recorded totals; multiple cumulative results must
not be summed. Session resets require accounting for each segment separately.

Claude's cost figures are local estimates, not authoritative Console charges.
Missing or crash-zeroed results do not establish zero spending. Preserve the
receipt and mark the invocation unresolved; block further admissions until the
cost is recovered or an operator records a correction with an explanation.
`budget --recover --note TEXT` reads durable receipts from stopped bridges and
removes their staged keys. It skips live bridges. `--reconcile` refuses a
running session with a live heartbeat. A native process that could not be
started settles at zero; a process that ran without a reliable result remains
unresolved.
Do not release a pending estimate merely because its process disappeared or a
timer expired. If logging fails during an active session, let it finish, retain
its cost receipt, and block subsequent admissions pending reconciliation.
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
and process launch. Verify a full replay matches snapshots and that corruption
blocks new work without stopping active work. Verify fair waiting and pending
estimates prevent every worker from claiming the same money.

Use a fake long session whose cost exceeds its estimate and balance. Assert it
and its subagents finish, the balance goes negative, and later sessions wait.
Changing balance or rate during that session must never signal its process.
There must be no budget-triggered termination or Claude budget-cap argument.

Verify API credentials and cost receipts across host, Bubble, `_round`, manager,
reviewer, and external runner paths, including secrets redaction and cleanup.
Verify cumulative/subagent costs are counted once, failures can incur spending,
and missing costs require reconciliation. Exercise provider cooldowns separately
from funding waits. Retain existing subscription behavior and its checks.

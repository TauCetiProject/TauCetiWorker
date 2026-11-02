# Cheap live usage estimates and reliable local accounting

Give fleet status a useful estimate of spend so far for running invocations, refreshed independently of expensive historical preparation. Start with filesystem metadata: one `lstat()` of each active invocation's append-only usage journal, with no journal-content reads. Improve accuracy with a small running-cost summary where needed. Keep native receipts and the existing append-only budget ledger authoritative.

This is an implementation plan. Do not use the Anthropic analytics API or introduce provider polling, extra model calls, new credentials, or a new service. The first deliverable is the live estimate; it does not depend on an accounting rewrite.

## Evidence and current behavior

The November 1 investigation found 608 unresolved stopped invocations initially: 514 retained usage journals and 94 had none. Most stored responses were complete, but missing final receipts prevented complete conversation accounting. Lifecycle records included signals, aborts and timeouts; recovery logs also contained earlier disk-full failures. By the end of catch-up, 615 eligible stopped invocations had been settled as labelled estimates. This establishes accounting recovery, not reconstruction of exact missing expenditure.

Relevant current paths:

- [UsageJournal](../../tauceti_worker/usage_accounting.py) already prices response counters and writes content-free deltas to an invocation-specific `.jsonl`, followed by a complete `.json` checkpoint on every usage update.
- [Activity costing](../../tauceti_worker/activity_costs.py) already includes observed live token costs. It rereads and replays the ledger, checks usage paths across the historical population, and recomputes contributions during background preparation. A fast status read can consequently display old live costs.
- [The usage supervisor](../../tauceti_worker/usage_supervisor.py) already tracks registered invocations and durable lifecycle evidence outside the round process group. Reuse that knowledge rather than rediscovering live work by scanning every historical directory.
- [Recovery](../../tauceti_worker/budget_recovery.py) already provides the stopped-usage policy, evidence checks and append-only corrections. Local changes prepared during the investigation release the accounting lock during previews and remove unnecessary historical calculations; those changes are separate from the implementation proposed here.

The [earlier fleet activity plan](fleet-activity-reliability.md) explains why per-invocation cache signatures alone cannot preserve global response deduplication, cumulative corrections and changing worker scopes. Keep those constraints. In particular, do not put a long reporting transaction on the budget's admission-owned `projection.sqlite`.

## 1. Ship a metadata-only live estimate first

### Use the invocation's append-only journal

Use `budget/usage/<bridge>/<invocation>.jsonl`, whose path follows directly from registered invocation identity. Its `st_size` is available without opening or parsing its contents. Record file identity and a starting byte count when the journal is created; this excludes its initial metadata-only record from chargeable growth.

Do not use the complete `.json` checkpoint's size: it is rewritten and mainly measures the number of stored response records. Avoid rendered worker-log sizes, which include diagnostics and deliberately abbreviated tool output. Native Claude transcripts may be worth comparing later, but locating them must use an explicit provider-session link; no directory search or worker/time matching belongs in the live path.

A usage journal's size is a proxy for conversation activity, not a token count. It grows with serialized usage updates rather than output text, and can include retries, duplicate events and different cache behavior. Calibrate it empirically for the exact journal format.

### Initial measurement supports a prototype

A read-only sample on tauchonki used 48 recent native-reported, positive-cost, non-resumed invocations from each of four phases. It read recorded costs from the disposable projection and obtained journal sizes with `lstat()`; it did not read journal contents. Within each phase, the older 32 observations supplied `median(cost / bytes)` and the newer 16 were held out.

| Phase | Fitted USD per MiB of journal | Median held-out error factor | 90th-percentile held-out error factor |
| --- | ---: | ---: | ---: |
| fix | 12.402 | 1.19 | 1.32 |
| roadmap | 19.777 | 1.17 | 1.35 |
| review | 10.919 | 1.33 | 1.78 |
| recovery | 7.252 | 1.16 | 1.89 |

Error factor is `max(prediction / actual, actual / prediction)`. The complete metadata-and-cost query took approximately 0.85 seconds for 192 invocations. This is a small, recent, phase-only experiment using total file sizes, not a validated live-prefix model or a cold-filesystem benchmark. Do not hard-code these coefficients. Refit with the starting-byte subtraction, model and format cohorts, then check live prefixes as described below.

### Estimate spend so far

For an ordinary non-resumed invocation, define:

```text
P = latest prepared token-priced or native-reported cost attributed to this invocation
B = current journal size
Bstart = journal size after its initial metadata-only record
r = cached median USD per byte for the matching model, phase and journal-format cohort

size_estimate = r * max(0, B - Bstart)
live_total_estimate = max(P, size_estimate)
live_unobserved_estimate = live_total_estimate - P
```

This estimates expenditure accumulated so far. It does not forecast the eventual cost of finishing the task. Never add `P` to an estimate of the whole file: that would count already observed work twice.

When a trustworthy consumed-byte anchor becomes available, use the more responsive form:

```text
live_total_estimate = Panchor + r * max(0, B - Banchor)
```

`Panchor` and `Banchor` must describe the same consumed journal prefix. A `stat()` taken after parsing is not a valid anchor: it can include bytes appended after the parsed prefix. Use an explicit writer offset or the reader's consumed offset. Until that exists, use the whole-file formula.

Use cached model-and-phase coefficients first, then a validated model-only cohort. Train on native-reported costs, excluding estimates, failed launches, resumes, missing files, zero-byte growth and incomplete evidence. Key coefficients by pricing regime and journal format so serialization or pricing changes do not silently reuse old coefficients. Refresh calibration as normal settlements arrive or during existing background preparation; never train inside `status` or on every sampling tick.

Require an observed usage-bearing response before enabling size-based spending for an invocation. Publish that marker from existing writer/supervisor observations; metadata-only initialization, user messages and retries must not manufacture a first charge. The sampler itself still needs no journal-content read.

For the first release, leave size extrapolation unavailable for resumes/replayed responses, unknown models, unsupported cohorts and ambiguous file identity. Continue showing their known attributed prefix and explicit missing estimate. Supporting resumes requires a trustworthy attribution/byte anchor; multiplying their entire journal size would charge replayed history again.

### Keep polling genuinely cheap

- Publish a small active-invocation view from the existing supervisor's already loaded lifecycle/session state. Include invocation ID, worker/scope, model, phase, journal path, file generation and prepared-cost anchor. Publish reporting metadata best-effort; it must not become an admission dependency.
- Sample this view once per minute in the existing manager, reusing its normal reporting cadence while remaining independent of the long-running activity-preparation subprocess. With 40 active invocations, this needs roughly 40 journal `lstat()` calls per minute. Spend reporting needs minute-scale freshness; add no faster polling loop. Do not enumerate historical usage, bridge or transcript directories.
- Publish a compact, disposable live overlay atomically without a durability barrier on every update. `tauceti-fleet status` continues reading prepared files; repeated CLI reads do not repeat collection.
- Store file identity, logical byte size, modification time and observation time. Missing, replaced, truncated, non-regular or unexpected-owner files invalidate the byte anchor; they do not imply zero expenditure. Preserve the existing explicit handling of remote/container ownership.
- Use file size, not allocated disk blocks. Compression, sparse allocation and filesystem accounting must not change the estimator's units.
- An unchanged journal freezes the byte estimate. It does not establish zero ongoing cost: an in-flight request may not have emitted usage yet. Expose the observation timestamp and stale/unknown state; do not accrue spend throughout arbitrary tool waits from session wall-clock time.

## 2. Integrate the estimate into spending without double counting

Include live estimates in the existing spending rows and their per-hour/per-worker-hour rates. Keep recorded and including-live values separate in JSON, with `live_observed_usd`, `live_unobserved_estimate_usd`, estimator provenance and observation timestamps. Healthy accounting adds no diagnostic lines; warn on unresolved stopped usage or stale live estimates, and omit an all-zero stopped-usage cost row.

The existing prepared total already includes some live observed usage. Apply only the difference between each invocation's live total estimate and its contribution in the matching prepared baseline. Never add a second complete live total. Reservations remain excluded, and reporting heuristics never settle the ledger, release reservations or change funding/admission decisions.

Version the overlay against its prepared baseline. When the historical report advances, rebuild/rebase the overlay's contributions against that generation. If files are read across a publication race, retain a consistent last-known report and retry normally; do not combine mismatched generations.

Keep terminal adjustments until the prepared baseline incorporates the corresponding settlement. Otherwise removing an invocation from the active view before its final cost reaches the report would temporarily lose its expenditure. A native final receipt replaces the heuristic even when the heuristic was higher.

Assign estimated growth to its sampling interval and split interval overlap across the existing reporting windows. Do not place an entire long-running invocation's cost at its latest poll. When priced response evidence becomes available, replace the covered provisional contribution; do not append it as additional spending. Keep the overlay disposable and retain only the intervals needed since its prepared baseline.

## 3. Add a tiny running-cost summary for accuracy

After the metadata-only estimate ships, have `UsageJournal` maintain its running priced total in memory, updating it by the difference between a response's old and new cumulative counters. Publish a small summary alongside existing usage evidence containing invocation and provider identity, sequence, observed time, running cost, completeness flags, pricing/format identity and the matching journal byte offset/generation.

The writer already receives the counters and prices them. A fixed-size summary read can supply that information directly without parsing the growing message dictionary or delta history. Compare this cost with the metadata-only path on the real host; prefer the summary where it gives materially better accuracy for negligible extra work.

The writer's raw running total can contain responses replayed by a resume. Mark that distinction explicitly. Only the host's existing response-attribution rules may turn it into an invocation-owned cost; a raw cumulative provider total must not bypass deduplication. Keep the size heuristic as a labelled fallback when a valid summary lags or is unavailable.

Preserve append-only evidence durability before forwarding usage events. Make full checkpoints periodic and on clean closure, rather than rewriting the complete message dictionary on every delta. Keep delta records synced; a reporting summary may be best-effort because the journal can reconstruct it. Ensure readers can recover a newer valid journal prefix when a checkpoint or summary lags, and measure suffix replay cost before increasing checkpoint intervals. Do not trade away evidence durability to make the display cheaper.

## 4. Make normal stopped recovery bounded and automatic

Reuse the existing usage supervisor and `stopped-usage-v1` policy. Process newly stopped invocations and changed late evidence, with deferred work retried in subsequent ordinary passes. Stable resolved history should not be reconsidered on every tick. Missing accounting must not prevent useful work or require an operator acknowledgement to resume.

Preserve the current receipt-first ordering, response-ID deduplication, exact process/boundary checks, labelled historical/prelaunch estimates and late native corrections. Never infer a stopped interval's endpoint from the recovery time. Live display estimates and journal size are not proof of process exit, complete usage or authoritative expenditure, and must not silently become stopped-settlement policy inputs.

Build each proposal from a small snapshot outside the admission lock. For committing, revalidate the affected invocation and provider-linked history, evidence identity and liveness, then append a short audited transaction against fresh ledger state. Concurrent unrelated fleet activity must not force an endless full-plan restart; changes to the proposal's actual dependencies require re-planning that affected group. A correction that touches a live successor remains deferred.

Use indexed native comparables for the relevant model and phase and retain the policy's seven-day-before-admission window. Cache equivalent rate inputs within a pass. Include the full audit inputs in retained manifests, but keep routine ledger events compact. Do not rebuild a fleet-wide manifest for each stopped invocation. Reporting and recovery failures retain evidence and retry; they do not pause admissions.

## 5. Reduce historical preparation only where measurement justifies it

Instrument ordinary preparation by ledger copy/parse/replay, usage metadata checks, changed usage decoding, allocation, table writes and summaries. Record elapsed/CPU time, bytes read, metadata operations, peak memory, report age and admission lock duration. Compare the same workload before and after; no paid model work is needed for measurement.

The fast live overlay must stay useful even while historical PR/LOC or cost preparation takes a long time. First remove redundant reads and avoid polling immutable native-settled journals when their final usage evidence and allocation are already cached. Preserve late receipts, missing evidence, resumed responses and changed scope dependencies.

If full replay remains material, extend the activity collector's own disposable cache to consume complete new ledger transactions by offset and sequence. Validate ledger identity and prefix continuity and rebuild automatically after replacement or repair. Keep first-settlement timestamps. Do not hold reporting reads on the budget's admission-owned projection or add another authoritative financial store.

Incremental allocation needs explicit dependency handling: a provider response has one owner; a resume or earlier cumulative correction can invalidate related invocations; worker-to-scope changes can invalidate historical scope allocation. Persist the required relationships in the existing activity database or retain the existing global recomputation for those cases. Prove equivalent results before replacing it. An invocation's own file signature alone is insufficient.

## Verification and acceptance

Extend existing accounting, usage-supervisor and activity tests for the changed behavior. Use synthetic evidence and retained local observations; make no paid agent calls.

1. **Cheap live path:** the sampler can update from journal metadata while opening journal/transcript contents is forbidden and historical preparation is stalled. The status reader consumes a published report. Adding settled history does not increase live sampling work.
2. **Accuracy:** collect byte-size observations during naturally running sessions and compare estimates at matched prefixes with later priced usage. Hold out later invocations, report per-model/phase signed bias and error factors, and include retries, large cache reads/writes, tool-heavy sessions, long in-flight requests and model changes. Final-size accuracy alone does not validate live-prefix accuracy. Publish a heuristic with its provenance immediately; do not present an unsupported confidence band as a calibrated interval.
3. **Attribution:** cover duplicate/replayed responses, resumes, interleaved subagents, earlier and later cumulative corrections, worker scope changes and replacement ledgers. No response is charged twice.
4. **Overlay accounting:** observed prefix plus provisional increment appears once; settlement arriving before/after a baseline refresh neither loses nor duplicates spend; native totals can revise estimates downward; window boundaries and generation races remain consistent. Reservations never enter spending.
5. **Failure recovery:** cover missing/shrinking/replaced files, stale summaries, disk-full writes, malformed/torn checkpoints and journal tails, SIGKILL, unknown remote liveness and restarts. Preserve known evidence and explicit unknowns. A stopped process or unchanged file never closes usage by itself.
6. **Routine recovery:** unchanged stopped evidence causes no repeated monetary changes; late native receipts correct estimates idempotently; provider-linked concurrency and live successors are protected. Proposals are prepared outside the admission lock.
7. **Performance:** demonstrate approximately one-minute live refresh under the normal 40-worker workload, metadata work proportional to active invocations, and bounded commit locks with no history-length-dependent planning under them. Repeated status reads must not trigger extra sampling. Measure summary-read/checkpoint tradeoffs and historical preparation separately. Failure of a reporting target must not block workers.

## Delivery order

1. Ship metadata-only live estimation, cached calibration, the lightweight overlay and clearly labelled spending/rate display. Retain observed-usage figures alongside it. Validate live prefixes during ordinary production work.
2. Add the small running-cost summary and reduce redundant full-checkpoint writes while preserving durable delta recovery. Prefer measured counters over heuristics wherever attribution is trustworthy.
3. Make ordinary stopped recovery process affected groups without full-history planning under the accounting lock.
4. Apply measured historical-cache optimizations with attribution and scope correctness tests; adopt incremental replay only if it addresses a remaining measured cost.

Use live runtime updates with worker and CI admissions enabled. Verify running revisions, useful completions, report freshness and any interrupted/stranded work after deployment, repairing problems in scope. Do not drain the fleet to obtain clean measurements. Rollback may remove a derived live overlay or rebuild a disposable cache; it must preserve journals, receipts, lifecycle evidence and committed accounting corrections.

Completion means the requested live estimate is visible and cheap even when historical preparation is slow, stopped accounting catches up without manual bulk recovery, final receipts replace estimates correctly, and collection does not interfere with useful work. Analytics integration remains outside this plan.

## Incremental accounting and operating cost

The manager retains two minute-scale tasks: a metadata-only live sampler and an
incremental historical preparer. Neither needs the analytics API. The sampler
uses one journal `lstat()` per active invocation and shares its result across
scope views; status readers only consume prepared JSON.

The preparer maintains an activity-owned SQLite projection of complete ledger
transactions. A durable change queue identifies affected invocations. Its cursor
is acknowledged only after the activity transaction commits, and its epoch changes
inside the projection rebuild transaction. A crash or ledger replacement cannot
lose a notification or preserve phantom spending.

Only changed invocations, currently live usage and bounded recent-final-checkpoint
retries inspect evidence. Response ownership is indexed by provider/message and
first admission order. A new or corrected owner reallocates the linked
invocations, without enumerating unrelated settled history. Attribution is frozen
per invocation; a worker changing scope does not reprocess its old conversations.
Native receipts can restate costs at their original response timestamps.

Report totals use persisted moving prefixes. Point costs and interval slopes enter
an indexed event stream. Advancing each reporting boundary visits only events
crossing that boundary; values for other subjects are evaluated lazily. Late
corrections update existing prefixes directly. Round counts, worker durations,
PR/LOC cohorts, contributions and CI totals use the same machinery. Prefixes and
dirty-fact acknowledgements commit before views are published. The complete PR
inventory is read from prepared SQLite only on an explicit inventory request,
rather than exported every minute.

A failed invocation rolls back its own preparation, retains an explicit retry,
and does not pin the successful ledger cursor. Known-incomplete preparation keeps
costs unavailable and produces a diagnostic until it succeeds. The whole cost
generation also has a savepoint for failures that cannot be isolated. Disposable
report aggregates carry a schema version; format changes rebuild them once from
retained facts. Initial migration and a lost/replaced cache may require a single
history scan, never a recurring full-history refresh.

The supervisor keeps its launch responsiveness and minute stopped-recovery pass.
Settled local recovery watches retire only with definite birth/liveness evidence
for shim, native, gate and owner, plus a stopped parent or an exact completed-round
fence. Unknown, malformed or unreadable identities and external producers retain
their late-receipt watch. Retirement archives evidence and changes no financial
settlement or cost provenance.

Validated measurements:

| Workload | Result |
| --- | --- |
| 40 active invocations, shared across 42 views | 40 journal metadata checks; approximately 13 ms for sampling |
| 100 / 1,000 / 10,000 settled invocations, idle refresh | 1,500 SQLite instructions in every case; approximately 1 ms for the scoped accounting check |
| 71,109 real invocations, 61 views, warm snapshot | Approximately 0.51 s overall, 0.024 s cost preparation, 36.4 MiB process peak; zero invocations reprocessed, zero usage decoded, zero ledger bytes replayed |
| Same real history, comparison with prior aggregation | All 61 views matched; maximum USD difference 4.5e-22 |

The real-history snapshot retained 23 active ledger entries, so the warm check
included 92 bounded metadata probes for that working set. Its ledger was frozen
for repeatable validation; late real completions were not committed to the test
copy. The first isolated migration took approximately five minutes and peaked at
142 MiB with the initial migration implementation. It is performed separately
from live report collection. Memory measurements are process high-water marks,
not an allocation measurement attributable solely to one accounting function.

Regression coverage includes 100x idle-history scaling, time-boundary inclusion,
intervals, late and downward corrections, response ownership changes, projection
replacement and crash recovery, invocation-local failures, cross-connection
prefix persistence, minute live sampling, publication races, terminal
acknowledgement and recovery-watch retirement with conservative liveness.

Deploy through live module/runtime updates and a prebuilt derived cache, keeping
worker and CI admissions enabled. Preserve the ledger, usage journals, lifecycle
evidence, native checkouts and other live patches. Verify revisions, report
freshness, useful completions and uninterrupted worker PIDs after activation.
Rollback restores reporting code and its derived cache; it does not undo audited
financial corrections. Analytics integration remains outside this plan.

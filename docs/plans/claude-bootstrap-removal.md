# Remove Claude bootstrap probes and allow useful work

Status: proposed implementation plan for TauCetiProject/TauCetiWorker.

## Outcome

A worker with eligible work should be able to use Claude when the only unresolved
condition is a recognized idle quota window and the other limits permit work.
Remove the dummy initialization request and its reservation machinery. Share valid
idle usage observations through the existing cache and refresh schedule.

Reliability and autonomous recovery are requirements: no new launch prerequisite,
mandatory telemetry request, persistent permit, wait condition, or configuration.

## Evidence

On 2026-10-31, worker4 received normal Claude responses to bootstrap probes at
22:30 and 23:30 UTC. It nevertheless stopped a selected review after the latter
probe. At 23:53, a diagnostic usage read returned HTTP 200, five_hour: null, and
weekly utilization 53%, under the configured pacing budget. The worker classified
this as unavailable because the successful bootstrap had not produced an active
session reading. The bootstrap record can suppress another attempt for up to one hour;
its expiry can simply repeat the same process.

Idle observations are excluded from caching, so the shared fetch lock serializes
requests without sharing their results. Five workers made 65 usage requests in
22:46–23:46 UTC: 64 returned 200 and one returned 429. This does not establish that
TauCeti caused every 429; the personal Claude system remains uncoordinated.

An offline reproduction against deployed release c94421883925d91ec926243256d23153b781a286
confirmed both behaviours without network or model calls. Investigation records:
/home/kim/.local/state/tauceti/claude-bootstrap-investigation/20261031/findings.json.
The provider's reason for continuing to report a null session is not established;
correct operation must not depend on that field becoming active after a probe.

## Policy

Keep the existing parser's distinctions. Recognized idle means an explicit null,
a recognized unopened representation, or a reset clock already elapsed. Missing
fields, contradictory values and malformed responses are different conditions.

| Observation | Required behaviour |
| --- | --- |
| Active window under pace | Existing availability and pacing behaviour. |
| Recognized idle window; all other windows permit work | Permit ordinary useful work, subject to the existing startup-spend policy for configured pace curves. No probe or positive-telemetry prerequisite. |
| Active window over pace | Preserve configured pacing and its existing explicit override. |
| Known exhaustion | Keep the current restriction and reset handling; idle data for a sibling window cannot override it. Preserve existing negative evidence during telemetry failures. |
| Missing or malformed quota data | Preserve existing handling; do not relabel it as idle or unlimited. |
| Usage endpoint outage or 429 | Preserve current telemetry-outage policy and HTTP cooldowns, including the existing explicit --ignore-quota behaviour. |
| Authentication or actual model-limit refusal | Preserve existing credential recovery and model retry rules. An idle usage 200 cannot prematurely clear a model-limit refusal. |

Apply the idle rule consistently to windows already recognized as idle, including
an explicitly idle weekly record, so removing the probe cannot strand an existing
supported case. Require every other actual constraint to permit the request.
Preserve deliberate zero-spend pace settings; introduce no new startup policy.
Do not invent a reset timestamp, pretend an idle window is an active 0% reading,
or claim that null proves unlimited quota. An idle weekly record supplies no
measurable weekly pace; status must say so rather than imply a measured budget.

The first useful request uses the ordinary agent path. Real provider refusals
still stop that attempt through existing handling. Normal work recovery and
publication remain intact; never replay a work unit whose completion is uncertain.

An idle observation can remain eligible after useful work begins until the normal
refresh, just as cached active headroom can lag actual spend today. It may permit
several workers to run; it is not a one-request allowance. Keep the existing cache
age and refresh bounds, report this tradeoff, and add no first-request permit or
initialization protocol to hide it.

## Implementation

Implement from the current upstream default branch. Compare with the deployed
release above: this worktree predates later outage and quota-wakeup fixes. Preserve
those fixes instead of copying this worktree's older quota implementation.

### 1. Remove the bootstrap dependency

Change the central Claude availability calculation once; provider selection,
--ignore-quota, parent loops and launch checks must consume the same verdict.
A recognized idle window should no longer be a hard availability failure when
the policy above permits first use. Update the hard/soft classification as well
as Provider.available: HARD_STATUSES, _unavail_reason, _ignore_quota_verdict,
_with_model_refusal and quota-status rendering must agree. Permitted idle is not
a hard block. A deliberate zero-spend startup policy still blocks through its
existing rule. In particular, idle plus weekly over-pace remains a soft pacing
wait, which the existing explicit --ignore-quota override may bypass.

Remove the dummy prompt/subprocess, bootstrap-only reservation ledger and locks,
episode/retry bookkeeping, bootstrap eligibility flags, provisional selection and
launch-stage handoffs. Retain account identity and credential-source helpers used
by normal usage sharing. Keep ordinary work selection, credential handling and
preflight checks. Include the security-audit launch caller, quota-wait bootstrap
constant imports, ledger observation and its _idle_notes patch in this cleanup.

New code ignores existing bootstrap records. Their presence, absence, permissions,
corruption or timestamps cannot affect availability or wake scheduling. No manual
file deletion or operator action is required. Remove their observation from the
local quota-wait path; retain side-effect-free local checks and normal absolute
provider-refresh deadlines.

Inspect parent/child command compatibility during rollout. If an old internal
bootstrap argument can reach a new child, keep --claude-bootstrap accepted as an
inert compatibility input while new callers stop sending it. Do not retain a
second permission path or require simultaneous fleet replacement.

### 2. Share idle observations without weakening cache validity

Reuse the existing payload format, account/credential identity rules, fetch lock,
cache locations, worker-state fallback, refresh interval and HTTP cooldowns.
Separate “valid observation to share” from “active window that can be paced”.
A well-formed payload containing recognized idle windows is shareable. This does
not make malformed or missing data a permissive cached observation.

Compute validity from the original observation time, capped by the existing TTL
and the earliest known reset of an active window. On cache reads, classify the
payload at fetched_at when deriving that deadline, including legacy entries
without valid_until. Do not use the read-time clock to turn a formerly active
window into idle before deriving expiry. Enforce both the stored deadline and
any earlier deadline established by the original observation. An idle window
supplies no new clock. A reset already elapsed in a newly received idle
observation must not make that observation immediately uncacheable. Conversely, an active cached reading
that reaches its reset expires: never reinterpret it as fresh idle permission,
even after the existing reset-skew allowance or while refresh is unavailable.

Pace active windows against the original observation time, as today. Time passing
alone cannot turn an old over-pace observation into permission. Preserve expiry,
identity changes, credential rejection and existing forced-refresh behaviour.
Fresh valid observations replace older usage observations through the existing
rules; add no separate negative-history ledger or latch. Peers must re-read the
shared cache after acquiring the existing fetch lock.

An unexpired actual model refusal remains binding even after an idle usage 200;
a successful telemetry status alone does not prove that refusal ended. Remove
_fetch_usage’s unconditional deletion of model-refusal records on HTTP 200; do not
retain it for active responses either. Keep the separate authentication-refusal
clearing on successful authentication. The existing model-refusal retry deadline
(currently at most fifteen minutes) must release the refusal automatically,
without requiring active telemetry or extending the deadline on each read. Preserve authentication recovery
and stronger known exhaustion independently.

Cache, lock, fallback-storage or diagnostic-write failure must not create a new
launch barrier. Use the existing degradation and retry paths. Do not add another
lock, leader election, polling service, background watcher, or retry protocol.
Coordinate only TauCeti's existing worker-owned state. Do not integrate with
personal ~/.claude scripts, histories, caches or coordination mechanisms.

### 3. Simplify status and documentation

Replace “bootstrap attempted” with the observed condition, such as “no active
session window reported”, alongside the real weekly/pacing status. Availability
should describe whether useful work can run. Report actual authentication, model
refusals and usage-endpoint failures distinctly.

Observation commands remain free of model calls. Remove obsolete bootstrap tests
and documentation assertions; retain the independent pacing, identity, cooldown,
credential, local-wakeup and recovery coverage. No new public setting is needed.

## Verification

Use fake clocks, temporary files and counted fake HTTP/model calls. Extend the
existing quota, shared-usage, loop, launch and quota-wakeup tests; no new framework
or live requests to provoke limits.

| Scenario | Required result |
| --- | --- |
| Session null and weekly under pace; eligible work | Ordinary work launches; zero dummy model calls and zero bootstrap records consulted. |
| Recognized idle weekly, or multiple idle windows | Same first-use policy, without another initialization state or permanent wait. |
| Idle session plus weekly over pace | Normal run waits on the weekly eligibility deadline; explicit --ignore-quota permits work with the soft over-pace verdict. |
| Default startup pace and explicit zero-spend configuration | First useful request permitted where the existing initialization policy permits spend; deliberate zero-spend policy preserved. |
| Old successful, failed, stuck or unreadable bootstrap record | No effect on selection, launch or wake scheduling. |
| Five workers, stable identity/store, repeated idle 200s and startup spend permitted | One shared fetch per existing refresh interval when sharing is available, rather than one per worker/poll. |
| Token rotation, account switch, reset boundary or fallback storage | Existing identity and invalidation rules hold; an appropriate fresh read remains possible. |
| Cached active reading reaches reset; fresh response is idle | Old reading expires, fresh idle observation is shareable, and useful work can proceed under the other limits. |
| Active cached entry beyond reset and its skew; refresh held by a 429 cooldown or transport failure | Entry is not served. Follow the existing telemetry-failure policy and applicable remaining restrictions. |
| Cached weekly reading was over pace | Advancing time without a fresh observation does not authorize work. |
| Idle session plus exhausted weekly limit; telemetry failure after known exhaustion | Preserve the applicable exhaustion and reset handling. |
| Credential rejection followed by valid authenticated telemetry | Existing authentication recovery works; an idle session introduces no further prerequisite. |
| Actual model refusal followed by idle or active usage 200, including a peer refresh | No deletion or premature permission; expiry permits normal retry without waiting for active telemetry. |
| Usage 429, network failure, malformed response or unwritable cache | Preserve existing outage/cooldown policy; no new request storm, mandatory write or permanent wait. |
| Parent/child handoff and local quota wake | Same eligibility decision; no extra model call, mandatory refresh or postponed normal recheck. |
| Actual model refusal after useful work starts | Normal bounded retry and recovery; no bootstrap fallback, duplicate publication or replay of uncertain work. |

Count requests with an unchanged identity across a complete refresh interval,
including simultaneous readers and the effective shared/fallback store. Also
test a failed refresh after a cached idle observation. The one-fetch target
applies when startup spend is permitted; do not broaden this change to redesign
the existing polling behaviour of deliberately zero-spend curves. Preserve normal
refreshes required at existing eligibility, reset or identity boundaries. Do not
turn a diagnostic request-count target into a runtime gate.
Run the repository's required checks and obtain an independent review focused on
missed wakes, cache/reset boundaries and preserving real refusal handling.

## Rollout and acceptance

Use the normal pinned-release and worker-manager deployment path, one worker
first, preserving normal drain/recovery handling and pending publication. Choose
a canary with eligible work when available; keep its existing work filters and
quota policy. Record the effective cache/lock location privately as well as
request counts: a credential fingerprint alone does not prove shared storage.
Rollout observation never pauses the rest of the fleet or adds a new drain
prerequisite.

Confirm useful Claude work can start under the observed idle-session condition,
with no dummy turn or wait for an active session display. A real limit or absence
of eligible work is an explained outcome, not a reason to bypass policy. Check
publication/recovery continuity and actual usage-request counts. Older readers
may reject idle cache entries and delete shared observations after their probes
during a mixed-version rollout; assess aggregate request reduction after the
relevant readers are upgraded. Record whether session/weekly windows become
active after useful work; continued null is an explained observation, not a new
condition to wait for. Watch actual model refusals and their existing retry rhythm,
including unchanged project error-budget/refund handling. Do not add a new
account-wide launch lease or coordinated retry protocol.

Proceed through the fleet after the canary and required checks are satisfactory.
Record release identities and observe at least one existing refresh interval.
Rollback a correctness or recovery regression through the normal release path.
Keep the cache format backward compatible, leave obsolete ledgers harmless, and
require no credential changes, file cleanup or persistent-data migration. Stop
any temporary observers and report the result and remaining limitations.

# Work assignment service specification

Build a transactional dispatcher that gives independent workers useful work without repository-wide polling, overlapping ordinary attempts, or repeated approval invalidation. It must improve the existing GitHub workflow before native contributions exist.

The service assigns work to worker harnesses and records their outcomes. Agents perform the assigned development or review task using the existing tools. Assignment, lease renewal, completion reporting, and interruption recovery are harness/service responsibilities; agents do not negotiate assignments or transfer tasks to other agents. Review continuity remains permissive.

Dispatcher availability must not become a prerequisite for running workers. On a dispatcher/network outage, active work continues and workers obtain new work through the existing selector and Git claims. Recovery reconciles fallback activity. Occasional duplicate work during a partition is preferable to stopping the fleet.

## Implementation handoff

Assignment bookkeeping and lease renewal belong in the harness, outside the model conversation.

Start with one complete path: import an eligible GitHub PR review, atomically assign it to one participating worker, invoke the existing review engine, and record its completion. The first implementation must also model the shared mutation group for `fix`, `fix-ci`, and `rebase`, and exercise their contention in tests. Live activation of those source-writing tasks can follow the review pilot.

The service implementation belongs by default in `TauCetiTools/services/work-assignment/`; the client/worker adapter belongs in this repository. Inspect current repository instructions and pinned shared-tool implementations before editing. Do not implement the service in this example harness merely because this specification lives here. Use an isolated worktree for a second repository if needed; preserve existing unrelated work.

This specification is self-contained. Use the existing review engine and its recorded results. Changing who assigns work must not reset approvals or trigger reviews merely because assignment ownership changed. The dispatcher records which work is needed and who is doing it; the existing tools perform the task and publish their results.

### First implementation task

1. Implement PostgreSQL migrations and a TypeScript API for work-item ingestion, worker sessions, claim, heartbeat, active-assignment lookup, result submission, completion, and lease-expiry recovery. Provide a local disposable database and documented API startup with fake-worker commands. Use bounded inline JSON context first; large payload storage can follow when needed.
2. Verify concurrent claims using independent database connections, lost-response retries, generation replacement, and process restart. These tests must exercise actual transactions and uniqueness constraints, not only a mocked in-memory scheduler.
3. Add an observer consuming the existing shared-state survey/facts and a Python `legacy|shadow|central` worker adapter, including automatic fallback selection and a durable fallback-activity journal. Initially run the review engine as one review task per PR; do not assume it can execute independent rubric assignments unless that capability is separately implemented.
4. Demonstrate a fixture workflow end to end without inference calls or GitHub writes, then prepare a shadow run and a concrete activation plan for selected existing workers/PRs. State which legacy selectors and claim/publication paths must honor that scope before activation. Keep normal admissions enabled.

The first task is complete when a local end-to-end demonstration passes, contention/recovery checks pass, and the observer/worker adapter plus shadow/pilot instructions are reviewable. Real production deployment and a paid live pilot are subsequent steps; they are not implied by a fixture test passing. The broader lifecycle, fairness, and recovery requirements below guide later increments and the full pilot.

### Existing code to inspect

Paths below are relative to this worker repository. Shared behavior may be supplied by the installed/pinned TauCetiTools and TauCetiReview releases, so inspect those dependencies rather than assuming an older sibling checkout is current.

| Entry point | Relevant behavior |
| --- | --- |
| [`tauceti_worker/work_units.py`](../../../tauceti_worker/work_units.py) | Candidate selection, stage dispatch, live revalidation, review invocation, source-repair execution |
| [`tauceti_worker/survey.py`](../../../tauceti_worker/survey.py) and [`survey_facts.py`](../../../tauceti_worker/survey_facts.py) | Work classification, eligibility facts, candidate preferences |
| [`tauceti_worker/github_state.py`](../../../tauceti_worker/github_state.py) | Shared-state reads, survey transport, unavailable/incomplete coverage |
| [`tauceti_worker/review_state.py`](../../../tauceti_worker/review_state.py) | Existing scoreboard and contest observations |
| [`tauceti_worker/round.py`](../../../tauceti_worker/round.py) and [`scripts/claim.sh`](../../../scripts/claim.sh) | Claims, heartbeat, round ownership, subprocess cleanup |
| [`tauceti_worker/loop.py`](../../../tauceti_worker/loop.py) | Model/provider admission, budgets, round supervision, background continuations |
| [`tests/dispatch_revalidation.py`](../../../tests/dispatch_revalidation.py), [`review_state_freshness.py`](../../../tests/review_state_freshness.py), and [`fixtures/survey_facts_parity.py`](../../../tests/fixtures/survey_facts_parity.py) | Existing behavior to preserve or deliberately replace with equivalent central coordination |

### Integration boundaries

Worker credentials and access matter to assignment: a worker allowed to review a public PR may not have permission to push a fix to its fork. The observer/session facts and local execution checks must preserve existing account, path, hold, and source-write restrictions. An advertised `lean-author` capability alone is not push authorization. Keep existing provider budgets and concurrency admission effective; do not build a new financial accounting platform for the first task.

The initial worker continues using its existing credentials and reviewed push/comment code. The dispatcher stores the actual completion and publication references; it does not need all contributors' GitHub tokens. A generalized central publication outbox can follow once that publisher's credential and ownership model is defined.

Generation checks enforce ownership of coordinator state. They cannot atomically prevent a stale or unmodified client from writing directly to GitHub. Preserve current branch compare-and-swap, claim compatibility, and publication checks, and document their limits. A pilot claim of one authoritative attempt applies to participating clients and selected work keys; it does not promise exclusive control over unrelated external contributors. Do not weaken existing write checks to make API-call counts look better.

## First deployable scope

The full pilot includes ingestion from existing GitHub/state-service observations, central assignment for selected workers, leases, continuations, evidence submission, and publication reconciliation through existing workers or a defined publisher. Start with reviews and PR fixes because they have stable work identities and visible duplication costs. Support authoring and staging through the same schema, but do not require their complete native workflows before the first deployment. Implement the narrower first task above before expanding the pilot.

Use PostgreSQL for assignments and an indexed ready-job projection. Cloudflare Workers serve command APIs; notifications wake workers through subscriptions or bounded long polling. No GitHub request is permitted on an ordinary claim or heartbeat. Missing upstream facts schedule hydration separately and remain explicit.

Use TypeScript for the Worker/API implementation and a Python client/adapter for this harness. Keep the dispatcher reusable in TauCetiTools or a dedicated service repository. The observer and publication adapter are separate modules with narrower external credentials; the pure eligibility/priority evaluator must be testable without GitHub or model calls.

## Assignment records and retries

Identify work by repository, PR number, and task kind; record the source commit used by each attempt. Give jobs, attempts, and worker sessions stable opaque IDs. A job can have several attempts over time. Each new assignment increments that job's generation so an old completion cannot overwrite its replacement.

Persist claims and completions before acknowledging them. Mutation requests carry an `Idempotency-Key`: retrying the same key and request returns the original result; reusing the key for different input returns a conflict. Use database time for lease expiry, initially renewing every 60 seconds with a five-minute lease. Failed renewal follows the outage behavior below rather than killing work.

An `authority_epoch` is a routing version for the selected work scope, incremented when normal assignment ownership changes between legacy and central selection. It protects service records from requests under an old routing configuration; it does not disable outage fallback. Authenticate worker requests with service-scoped credentials. Report service failure separately from an empty queue.

## Work identity and data model

| Record | Required fields |
| --- | --- |
| `work_item` | Stable ID, project/library/integration-unit IDs, kind, obligation key, origin, desired outcome, current input references, authority epoch |
| `job` | Work item, task kind, dependencies, priority band, ready/waiting state, earliest start, budget class, resource/capability requirements |
| `attempt` | Worker principal, generation, lease, chosen input snapshot, checkpoints, execution identity, cost/resource reports, result references |
| `finding` reference | Explicit unresolved scope and reason; not inferred from changed head |
| `worker_session` | Principal, capabilities, allowed libraries, available slots, subscription, harness/protocol version, last heartbeat |
| `publication_intent` | Expected external state, desired update, output reference, retry state, ownership epoch |

An obligation key identifies the logical work. Initially use `(repository, PR, review)` for one ordinary review-engine run and `(repository, PR, source_repair)` as the shared source-writing group. Later `(library, change, review_dimension)` keys are available when independently executable rubric tasks exist. An input revision is an attempt binding, not automatically a new obligation key. A moving branch must not create unlimited simultaneous fix jobs or repeated full review jobs.

Deduplicate observations using external event identity and coalesce updated desired inputs into the work item. A running attempt keeps its selected input unless the existing execution path updates it as part of doing the task. Record the actual inputs and outputs through the harness. Preserve new findings as context without terminating useful model work because a webhook changed a timestamp.

Source-mutating tasks on the same contribution share a mutation group, even when their task labels differ (`fix`, `rebase`, `ci-repair`). Only one ordinary authoritative source writer holds that group. The first worker adapter claims a whole review-engine run rather than launching overlapping complete runs under different rubric keys.

Roadmap area and module overlap are soft signals. They must not become exclusive locks on whole subject areas or every file an agent might inspect. Use exact target reservation for duplicate authoring intent, and allow agents to explain intentional complementary work.

## Worker protocol

```http
POST /v1/worker-sessions
Idempotency-Key: <uuid>
```

```json
{
  "worker_id": "operator-machine-worker-3",
  "protocol_version": 1,
  "capabilities": ["lean-author", "review"],
  "library_ids": ["core", "subject-a"],
  "max_active_attempts": 1,
  "preferences": {"task_kinds": ["review", "fix"], "affinity_change_ids": []}
}
```

The service authenticates the principal separately and intersects requested access/capabilities with its policy. It does not receive provider credentials or choose a worker's subscription account. Session identity is stable across reconnects for the same harness process lineage.

Use service-scoped worker credentials with library/action scope for the first deployment. A configured pilot cohort does not establish a new reviewer reputation or maintainer-approval requirement. Authentication verifies the caller's identity and allowed operations; it does not change the project's permissive review participation policy.

`POST /v1/assignments/claim` accepts `session_id`, requested free slots, accepted task kinds, optional target, and `wait_seconds` up to 25. The initial API returns at most one assignment unless the session explicitly supports concurrent attempts. A response contains:

```json
{
  "job_id": "<uuid>",
  "attempt_id": "<uuid>",
  "generation": 3,
  "authority_epoch": 8,
  "lease_expires_at": "2026-11-01T23:05:00Z",
  "task": {
    "kind": "review",
    "library_id": "core",
    "source": {
      "kind": "github_pr",
      "repository": "TauCetiProject/TauCeti",
      "number": 123,
      "head_sha": "<full Git commit SHA>"
    },
    "context": {"finding_ids": [], "review_summary": "<bounded current review facts>"},
    "desired_outcome": "Resolve the reuse finding or preserve the existing approval",
    "budget_class": "ordinary-review"
  }
}
```

Context contains contribution intent, actual source bindings, findings and replies, prior approvals, applicable instructions, relevant dependency facts, and continuation references. Large content is immutable and paginated. Workers should not re-fetch the same data from GitHub for normal dispatch preparation.

For the GitHub pilot, repository/PR and the observed Git commit identify the task inputs. Source retrieval uses existing Git preparation. Use bounded inline context containing the available facts; unavailable observations remain explicitly unknown.

| Endpoint | Purpose |
| --- | --- |
| `POST /v1/attempts/{id}/heartbeat` | Renew and report execution state; return coalesced relevant updates |
| `POST /v1/attempts/{id}/checkpoints` | Persist resume context and candidate snapshot without declaring completion |
| `POST /v1/attempts/{id}/results` | Append evidence, patch, findings, cost, or proposed follow-up work |
| `POST /v1/attempts/{id}/complete` | Finish the current authoritative attempt with a typed outcome |
| `POST /v1/work-items` | Contributor/agent proposal for new work, subject to deduplication and resource policy |
| `GET /v1/work-items/{id}` | Explain current owner, waiting conditions, attempts, evidence, and next action |
| `GET /v1/worker-sessions/{id}/assignments` | Recover this session's active assignments after reconnect or a lost claim response |
| `POST /v1/fallback-attempts` | Idempotently import journaled fallback work, actual input/output references, and current running/completed state for reconciliation |
| `GET /v1/events?after=...` | Resume scoped updates |

Lease/generation/epoch accompany authoritative mutations. A timeout after the claim command must be retried with the same idempotency key, or the session queries its active assignments; it must not blindly claim another slot.

## Claim transaction

One transaction must:

1. Validate session/principal state and the library authority epoch.
2. Select an eligible ready job using indexed predicates and policy priority.
3. Lock the job row using `FOR UPDATE SKIP LOCKED` or an equivalent atomic claim.
4. Acquire its mutation-group reservation when applicable, and reserve budget/concurrency capacity.
5. Recheck eligibility and uniqueness after acquiring the relevant rows.
6. Increment the generation, create the attempt and lease, update job state, and insert its outbox event.

Do not hold a transaction across network requests or model execution. Use a fixed ordering for session, capacity, and mutation-group row locks to avoid deadlocks. Enforce uniqueness at the database level; process-local mutexes are insufficient. The transaction must not lock every job in a library while assigning one review.

A partial unique index or equivalent constraint ensures at most one ordinary active attempt per obligation/mutation group. Review dimension groups may be independent. Speculative duplicate work is explicit, budgeted, and linked to its parent rather than an accidental consequence of different worker views.

## Scheduling policy

Hard eligibility includes supported capability, available execution slot/budget, dependency completion, explicit holds, and authority. A cached “eligible” flag is a selection hint; verify the authoritative conditions before claim.

Priority bands initially order operator/staging repair, existing-contribution repair/review, dependency adoption, and new authoring. Use configurable shares and aging across bands and libraries so continuous emergencies do not permanently starve accepted ordinary work. Reserve a staging/operator share and borrow idle capacity. Affinity to a prior reviewer is a positive preference, not a mandatory first-refusal wait or exclusive right.

Allow project policy to change priorities live at assignment boundaries. Do not cancel a paid active attempt merely to apply new preferences. Explicit operator cancellation remains available with checkpoint retention.

Automatic head movement does not reset review jobs or their age. When a new source revision arrives, retain effective approvals and attach the delta. The harness records the outcome of the current task; the service determines remaining eligible work from updated source and review facts. Concrete blocking findings can stop the relevant acceptance transition while leaving unrelated work eligible.

An unavailable observation source is different from an empty ready queue. Dispatcher unavailability activates the fallback path below; it must not simply return “no work” or stop new rounds. Work whose source is already captured can continue during a GitHub outage. GitHub publication intents wait independently. Legacy jobs requiring fresh external write authorization wait on that specific condition instead of surveying the entire repository again.

## Dispatcher outages and recovery

Central mode normally asks the dispatcher for assignments. After a bounded reachability check identifies transport failure or service unavailability, the harness enters a visible `central-fallback` runtime state and uses the existing selector, shared-state/cache reads, and task-appropriate Git claims. This is automatic; it does not require an operator to change every worker's configuration. An ordinary empty assignment response is not an outage. Explicit cancellation, permission denial, and spending limits retain their meanings and must not be bypassed as availability failures.

An active model session/tool call keeps running when heartbeats cannot reach the dispatcher. The harness journals progress, attempts renewal in the background, and preserves results for later submission. A missed central lease renewal alone must not kill the agent or prevent its next useful action under fallback coordination. Existing GitHub push/comment checks and local budgets still apply.

During the GitHub pilot, central and fallback execution both use the existing task-appropriate Git claim paths so they can see each other's work when that mechanism is reachable. Fallback prefers cached context and known unfinished work and staggers refresh/retry traffic. It must not create a GitHub polling surge or ignore actual upstream quota cooldowns. If central and Git-based coordination are both unavailable, use the existing best-effort claim behavior and avoid known overlap; global duplicate-execution exclusion cannot be guaranteed in that situation.

Journal a stable local attempt ID, optional central job/attempt identity, work key, actual source revision, start/completion state, output/publication references, and cost observations before losing that information to another round. A timed-out claim request retains its idempotency key. The harness must not later launch that request as a second assignment after it has already moved on through fallback.

On reconnect, import journaled activity idempotently and refresh relevant source/review facts before reissuing those work keys. Adopt live fallback claims, recorded completions, and useful outputs; do not reset approvals, rerun completed reviews, or abandon repairs because their central lease expired. Reconciliation is scoped to affected work, so other assignments can continue. If two attempts overlap, retain both outputs and let the existing source-publication rules and agent judgment reconcile them. Generation fences still prevent an old attempt from blindly overwriting newer coordinator records.

Retry central connectivity with backoff/jitter and return automatically to normal central selection after reconciliation. Report time in fallback, useful completions, coordination collisions, duplicate execution, and reconciliation failures. The service improves coordination in normal operation; continued work during a partition is an explicit requirement.

## Outcomes and recovery

An authoritative completion is one of `succeeded`, `needs_followup`, `waiting_external`, `retryable_failure`, `declined`, or `cancelled`. The service maps these to job state and new obligations according to task policy. A contribution is not abandoned merely because one attempt fails or reaches its model budget.

These are harness-reported outcomes, not an agent handoff protocol. `needs_followup` records incomplete work and retained outputs; the service uses updated eligibility facts to make the remaining work available. It does not require the agent to select a recipient or write a handoff. `waiting_external` names a concrete observation or operator decision. `declined` requires a reason and releases the attempt without converting missing data into no work. Repeated declines become visible scheduling feedback.

On expiry, retain outputs and mark the attempt expired. Prefer resuming existing work before starting fresh. A late result is accepted with `authoritative: false`: it is retained but cannot overwrite a newer attempt's state. Reconcile it with the actual PR, published review, and running worker before deciding whether more work is needed. The API must distinguish successful result retention from rejection of a stale mutable completion.

Budget reservation and settled cost are separate. If an attempt loses connectivity after paid work, retain an unsettled-cost entry for later reconciliation; do not silently count it as free or bill it twice. An unavailable accounting report does not cause already-created source to be discarded.

## Existing worker and GitHub adapter

Add explicit worker backend `legacy|shadow|central`, selected per worker and work key. Shadow records proposed selection without starting duplicate agents or publishing claims/comments. Central workers normally claim from the service and automatically use legacy selection/claims during dispatcher unavailability, as specified above.

A GitHub observer maps PR IDs, heads, review metadata, replies, CI observations, and repair requests into work items. Use the existing state service when its coverage is available; coalesce direct reads needed for uncovered resources. Incomplete observations remain unknown. There is one hydration obligation per resource, not one per interested worker.

Before activating a work key, inventory all known legacy mutation paths and make normal selection honor the chosen routing. Keep the fallback path available during dispatcher outages; a cached central-ownership setting must not permanently exclude that work when its coordinator is unreachable. Existing external contributors may still submit work. Branch compare-and-swap plus result reconciliation preserves edits without pretending all third-party clients participate in central leases.

Initially the existing worker owns push/comment publication with its current credentials and records a durable local intent/result for reconciliation. Do not route publication through both that worker and a new central writer. A later publication adapter may hold narrowly scoped external credentials and own a durable outbox. Both paths must check expected GitHub/source state and reconcile lost responses without another model round. Public comments should reflect meaningful state changes rather than every lease heartbeat.

The first central pilot can retain legacy Git claims as compatibility with existing review/source writers. Those claims still matter to external publication paths that have not adopted coordinator ownership; do not simply bypass them after obtaining a central lease. The dispatcher is authoritative for assignments to participating clients. Claim migration documentation must state the remaining limits for unmodified external workers and identify when the legacy claim can actually be removed.

For the pilot, keep those Git claim paths active during both central and fallback execution. Removing them later requires an independently available fallback coordination mechanism or an explicit decision to accept more duplicate work; it must not silently change outage behavior to stopping workers.

## Metrics and benchmark contract

Measure assignment latency, eligible-to-start age, duplicate ordinary executions, reassignment count, useful late-result adoption, paid time lost on interruption, review reuse, per-library fairness, GitHub calls per completed job, and reasons for waiting. Log stable job/attempt/change IDs across existing worker receipts.

Initial load objectives are 1,000 sessions, 10,000 ready/waiting jobs, and 100 assignment commands/second with p95 command latency under one second at the API boundary in the benchmark region. Exclude caller-selected long-poll wait. At most one ordinary central attempt may win a contested key, regardless of client concurrency. Partition/fallback tests measure continued useful work and any duplicates separately; the central exclusion guarantee does not imply globally exclusive execution while coordination is unavailable. Publish workload, database size, contention distribution, and latency percentiles with results.

Run synthetic scheduling/failure replay without model calls. Then observe naturally selected pilot work. Expansion requires lower duplicate paid work and fewer GitHub selection reads without reduced useful completion rate. A low assignment latency alone is insufficient.

## Deliverables and acceptance

Deliver API and migrations, worker client and selector adapter, observer/hydrator, publication reconciliation through the declared owner, authority migration tool, telemetry, and restore/rollback runbooks. A central publication adapter is required only when that implementation takes over publication. Specify which service owns every job kind during the pilot.

Acceptance scenarios:

1. One hundred workers claim the same fix simultaneously; one ordinary source-mutating attempt starts.
2. A fix and rebase for the same contribution contend on the same mutation group. Whole-engine review tasks cannot accidentally overlap under different rubric keys; genuinely independent review tasks can remain concurrent when supported.
3. A lost claim response returns the original assignment on retry and consumes only one slot/budget reservation.
4. A source update retains approvals and attempt context, and does not enqueue a full review reset.
5. A disconnected worker's late patch/review can be adopted without granting it authority to overwrite current work.
6. Restart resumes a checkpoint and preserves cost evidence and job age.
7. A failed GitHub read cannot become “no work”; a known captured job continues during a GitHub outage.
8. Operator/staging capacity remains available under authoring load, and another library still receives its configured fair share.
9. Migration and rollback transfer one work key through new epochs without draining workers or duplicating publication.
10. Database restore and event replay preserve one owner, ready work, and pending publication intents.
11. Dispatcher failure or worker-to-dispatcher network loss leaves active tool calls running and allows new useful work through the existing selector/claims without an operator toggle.
12. Recovery imports fallback work before reassigning affected keys, preserves reviews/repairs already completed, and resumes central selection without a fleet pause. A lost claim response cannot start a second local round after fallback has begun.
13. An outage does not cause a polling surge or bypass explicit holds, credential restrictions, provider budgets, or actual GitHub quota cooldowns.

Deploy in shadow first, then activate selected keys with normal routing and the automatic fallback path in place. Let already-running rounds finish and reconcile their claims/results before assigning the same work again; no fleet-wide idle point is required. Test dispatcher failure before expanding the pilot. Roll back normal routing with a new ownership epoch and preserve running work, fallback journals, evidence, and approvals.

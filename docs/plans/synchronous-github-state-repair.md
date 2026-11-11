# Repair contradictory GitHub state synchronously

## Objective

When a worker discovers that shared GitHub state contradicts GitHub, correct the shared record synchronously and let the worker select useful work. A successful repair response means that the correction is committed and available to subsequent readers, including surveys. A failed repair must leave ordinary autonomous recovery working.

Prefer small changes to the existing service and worker over a new consistency framework. Keep centralized GitHub reads, quota ownership, webhook ingestion, storage, and background reconciliation. Add no persistent “disputed” classification, repair acknowledgement workflow, operator reset, new service, queue, scheduler lane, or general-purpose field-version registry. The goal is to remove demonstrated failure paths without creating dependencies that strand work.

This is an implementation plan, not a record of deployed changes. An implementation cannot promise that failures are impossible; it must demonstrate bounded failure handling, automatic recovery, and acceptable throughput before rollout is considered complete.

## Evidence

The November 1 investigation found repeated review-start failures on four PRs. In the 10:04 UTC activity snapshot, 114 of 120 failures were head mismatches; none were recorded as timeouts. Four more failures came from one worker's empty review ledger, and two from an agent exit and checkout failure. The ledger problem is separate from this plan.

For PR #24859, the archived `labeled` webhook contains head `873e7553d05d` and PR `updated_at=2026-11-01T09:59:13Z`. GitHub's API returns head `e9421ca89880` and `updated_at=2026-11-01T09:23:46Z`; the branch ref corroborates that API head. The server accepts the label event's entire PR snapshot, then rejects a fresh API correction because its PR timestamp is older. A local reproduction against the downloaded production bundle confirms that rejection. The origin of GitHub's inconsistent representations remains unexplained; correcting the service must not depend on explaining it.

The repair publication path can ignore a rejected resource update and still advance collection verification and finish the job. Existing client demand promotes missing coverage but has no explicit correction path for a complete yet incorrect record. Worker review preflight can read the same mirror again, and the final review command's head check then fails, charges an error, and causes backoff.

At one investigation sample, approximately 6,000 repair jobs were due while both GitHub pools retained more than 6,200 of 6,300 points. Record the current baseline again before implementation; do not assume quota exhaustion or that increasing concurrency fixes publication bottlenecks.

Investigation artifacts currently reside in `/tmp/tauceti-state-*.json`, `/tmp/tauceti-state-deployed-20261101.js`, and `/tmp/tauceti-state-ordering-repro.mjs`. These are temporary evidence, not runtime dependencies. Preserve minimal sanitized fixtures in the owning repositories' tests.

## Required behavior

1. A mismatch is a reason to correct shared state and reselect work. It is not a failed review of the PR and does not consume its review-error allowance.
2. Repair uses a targeted GitHub read under the service's existing installation scheduler and authentication. A worker's observation identifies the contradiction; it is not blindly written as authoritative state.
3. Success follows atomic publication. Rejected, interrupted, or uncommitted corrections must not be reported as verified repairs.
4. Concurrent requests for the same PR share work where practical. Network waits never hold a database transaction or repository-wide writer lock.
5. Timeout, quota exhaustion, a lost response, service restart, and worker restart require no acknowledgement or manual intervention. Existing background repair remains the recovery path.
6. A failure concerning one candidate does not suspend the whole fleet or permanently exclude that PR. Other candidates and independent authoring remain available under existing admission rules.
7. Keep the execution-time expected-head check. A push can race even a successful synchronous repair; freshness cannot provide an exclusive claim on a GitHub branch.

## 1. Correct ingestion and publication first

Implementation belongs in [TauCetiTools' GitHub-state service](https://github.com/TauCetiProject/TauCetiTools/tree/main/services/github-state), principally `repository.ts`, `normalize.ts`, and their focused tests.

### Stop unrelated events from replacing the head

Handle PR webhook actions according to what they actually change. In particular, a label event changes only the named label; its attached PR snapshot must not replace the head, base, lifecycle, or unrelated scheduling facts. Keep explicit handling small: head/lifecycle events, label changes, edited fields, and draft changes. Unknown actions schedule targeted reconciliation instead of replacing a whole record. Do not build a generic event-sourcing or field-version system.

Head-changing and lifecycle events retain their appropriate update paths. Unknown actions and events without a usable baseline schedule the existing targeted PR reconciliation; they must not invent complete coverage, erase known facts, or silently stop being reconciled. Preserve delivery deduplication and existing tombstone behavior.

### Allow a fresh read to correct the stored value

Remove the assumption that a greater generic PR `updated_at` makes every field authoritative. A targeted API read that began after the relevant stored input must be able to correct the demonstrated poisoned head despite the lower source timestamp. Preserve source timestamps as evidence; do not fabricate newer timestamps to defeat the old comparison.

Keep the existing conservative row-level observation fence: any newer stored input, including labels, can supersede a read. Use the actual targeted HTTP-read start, not a job start retained across quota waits. On supersession, re-arm the same job with a fresh start and no ETag; do not retry inside the request. This avoids introducing field-level provenance. An equivalent full record may count as validated despite a concurrent re-observation. Never remove ordering checks for other resource types.

Keep this logic in one correction/publication helper shared by ordinary PR reconciliation and synchronous repair. Its result must distinguish an applied or equivalent read from a read superseded by a newer relevant input. If a read loses that race, preserve the winner and the existing reconciliation obligation; do not call the rejected value verified. Use a retryable response when the synchronous operation cannot establish a valid result within its request budget.

### Bind conditional reads to the stored representation

A GitHub ETag validates the GitHub representation previously fetched, not a different row subsequently written by a webhook. In the same transaction as a changed PR write, clear its targeted job and collection validators. Never retain a new ETag from a body rejected during publication. Reject an in-flight 304 if the stored representation changed since that request began; re-arm a full read instead of advancing verification. The synchronous endpoint always requests a full body.

Use a one-time automatic metadata-version migration to clear existing PR validators, including validators held by already-claimed jobs. Clear verification timestamps that the former rejected-body/304 path could have advanced without validating the stored record. No manual SQL step is required. Preserve actual upstream cooldowns and leases. Background repair must recover pre-existing poisoned records even if no worker reports them.

### Publish the correction everywhere scheduling reads it

In the existing publication transaction, update the canonical PR, the compact survey inventory, head associations, and affected membership/lifecycle facts. Advance the existing revision used by conditional reads and cached survey snapshots. Explicitly evict the completed-query grace cache on correction; an older in-flight query must not repopulate a grace entry that subsequent readers can use. Preserve useful history and immutable commit-addressed data.

A changed head must invalidate or dirty the existing head-dependent file/status coverage through the normal reconciliation paths. Do not block the repair response on fetching comments, files, statuses, or a repository inventory. Those collections remain explicitly incomplete or pending as appropriate; old-head data must not be presented as current-head coverage.

Check all existing PR read paths and aliases so one stale copy cannot overwrite or outlive the corrected canonical record. Subsequent new queries must see the published correction. Existing paginated snapshots may remain internally consistent historical snapshots; workers must validate a selected candidate rather than treating a cursor as current authority.

## 2. Add one narrow synchronous operation

Add authenticated `POST /v1/refresh-pr` using the existing repository-scoped client credentials. Initially support only targeted PR detail refresh. Do not add arbitrary GitHub URLs, SQL, generic patch payloads, or synchronous full-history collection. Keep ordinary cached query behavior unchanged.

Request fields are the repository, PR number and expected head, with an optional observed head. If the stored head already differs from the expected value, return the committed current record with an explicit already-changed outcome, without spending another GitHub read or claiming a new validation. Reuse a full verification for at most 30 seconds only when its recorded revision still covers the same canonical record; a newer webhook invalidates this shortcut. Those heads provide diagnostics and allow stale reports to be recognized; they do not authorize writing client-provided values. Reuse existing response conventions for the PR record, published revision, validation time, and retryable unavailability. No new persistent resource classification is needed.

Execute the existing installation-owned `pr` read directly, then use the shared correction helper. “Directly” means it need not drain the repository's background history queue; it still obeys actual quota, concurrency, and secondary-limit waits. Reuse the existing catalog, transport deadlines, and scheduler admission. Keep the endpoint narrow enough that adding it does not require redesigning background scheduling.

Share an in-flight refresh per repository/PR within the existing repository owner. Keep this as a small request-lifetime promise map if there is no suitable existing primitive. Remove entries on either settlement path; expire a stalled shared flight after one minute. A replacement read remains safe because the late response must pass the same revision fence. A rejected or hung promise must never own future requests permanently. A restarted instance simply starts another safe read. Do not add distributed leases or durable synchronous request ownership. Background and foreground overlap may occasionally duplicate one read; that is preferable to new coordination capable of stranding repairs.

Wait for the actual shared read and committed publication result. Add no separate response or client socket deadline for repair: the worker already has a round deadline, and existing upstream/database transport failures remain explicit. A caller disconnect may leave a safe publication completing under the platform's existing request-lifetime facility; it must not own a new durable lease. Use the existing writer's non-publication admission so grouped history publication yields to the short correction. Do not nest retry loops or wait through a quota reset. If capacity or quota prevents prompt completion, return the existing retry information and retain/enqueue the ordinary targeted repair job. Repeated reports must not reset a real retry deadline or continually restart an in-flight background read.

Return refresh retry deadlines in the response body, never a `Retry-After` header. The worker refresh helper must not call ordinary `read()` or change its identity-wide circuit or cooldown. Only the affected PR/head receives a local retry deadline, stored through existing worker counters and bounded by the client's existing maximum retry interval so a malformed distant timestamp cannot strand it. Early rechecks still obey the installation scheduler's real quota restriction. Unsupported endpoint responses (400/404/405) are ordinary optional-capability failures.

Client disconnect does not roll back a committed correction. Repeating the request after an ambiguous response is safe. If the server cannot commit, return failure and retain previous facts; never acknowledge a queued job as a synchronous success. There is no client acknowledgement after publication.

During the fetch, ordinary readers may still see the previous cached value. Accept this short race; do not add reader locks or block whole surveys. Successful publication ends it for subsequent new reads. The worker's final head check handles races and readers already holding older snapshots.

## 3. Connect worker mismatches to repair and reselection

Implementation belongs in `github_state.py`, `work_units.py`, and existing candidate/round handling in TauCetiWorker. TauCetiReview exposes its head mismatch through a distinct non-zero exit and the existing result file. Never use exit zero for a mismatch: older workers interpret it as a published review. Do not make long-term correctness depend on parsing English stderr. Preserve the expected-head refusal and genuine review-engine failures.

Make the selected review head check direct, matching the existing selected fix/rebase checks; only that head check needs the direct context. This adds no survey-wide read and detects stale selection before engine setup. On a selected-PR contradiction observed by preflight, checkout, or the review command:

1. Request one synchronous shared refresh for that candidate through the common client helper.
2. Invalidate the relevant local PR/survey cache entries using existing cache mechanisms.
3. Return to normal candidate selection. Do not merely replace `c.head` and continue: review eligibility, contests, claims, and file facts may have changed too.

Use the existing cascade, which already offers each candidate once per survey; add no duplicate attempted-candidate set. Filter only the affected PR/head until its retry deadline, and revisit it automatically afterward. If all offered candidates changed or await refresh, use the existing temporary-candidate wait exit rather than the no-progress failure path. Other candidates and roadmap authoring remain available first. If a successful server validation confirms the same head that the worker contradicted, use the existing maximum retry interval for that PR/head rather than immediately repeating both reads. Log this repeated source disagreement explicitly. Release existing claims through ordinary cleanup even when refresh fails. Do not add a persistent quarantine list or a new worker state machine.

Missing endpoint support, authentication/configuration failure, timeout, and transport failure must not escape as a charged PR error. Preserve the worker's existing direct-read fallback policy where it applies; do not introduce a fleet-wide switch to direct reads, or an additional GitHub retry loop. Record a concise diagnostic, respect any supplied retry deadline with existing scheduling facilities, and continue independent work. If no candidates remain, use the existing idle/wait path rather than polling the bad candidate in a tight loop. When the service returns, ordinary work must recover automatically.

Before survey suppression and stuck-issue handling, refund only retained exit-1 attempts containing the review CLI's exact PR-number/head-mismatch message with distinct hexadecimal heads, preserving genuine errors. The coarse `stale-head` category or generic words such as “expected head” are insufficient evidence. Make the adjustment restart-idempotent: record the intended before/after counter values in that same diagnostic file, apply the counter update, then remove the refunded attempts and adjustment receipt. Resume any unfinished adjustment before accepting new attempts. Do not infer refunds from unknown older history or blanket-reset counters. Existing stuck issues can remain historical diagnostics; closing them is outside this release. New mismatches never increment the counter.

Keep mismatch repair separate from failure to obtain any discovery data. This endpoint must not become a new compulsory admission dependency for roadmap authoring, status reporting, or all survey rows. Launch-time validation may reuse it where a direct selected-PR check already exists, but adding a mandatory network read before every healthy round is not required for this release. Measure that separately if needed.

## 4. Preserve autonomous recovery and throughput

Use the existing targeted job as the durable retry mechanism when synchronous repair cannot finish. Promote it into the existing requested lane even when the old collection is marked complete, and retain that priority across later webhook enqueues until the obligation completes, while respecting active leases, newer generations, actual upstream waits, and normal publication guards. No record waits for a worker to return and acknowledge its repair.

Reserve no additional scheduler lane or fixed quota pool initially. The synchronous operation uses the existing installation admission path, and short publication uses the existing writer admission. Verify that it makes progress under a representative history backlog. If measurements reveal starvation at an existing admission boundary, make the smallest change there; do not introduce a second scheduler, distributed refresh coordinator, or broad priority framework speculatively.

Keep ordinary repair useful: automatic API reads must also correct poisoned records even if no worker reports them. A missed report, unsupported old client, process crash, or failed foreground request must not turn into permanent incorrect state. Reporting accelerates convergence; it does not own it.

Extend existing diagnostics only: synchronous attempts, coalesced callers, published corrections, superseded reads, failures/retry reasons, latency, and repeat mismatches for the same PR/head. Distinguish successful GitHub transport from successful publication. Reuse existing logs and health preparation; add no monitoring service or growing report history.

## Verification

Run focused service, worker, and review-command tests without paid inference. Use the captured mismatch as a regression fixture and exercise actual publication/query paths, not just a replacement helper returning success.

| Scenario | Required result |
| --- | --- |
| Label snapshot has a wrong head and later PR timestamp | Label handling preserves the head; both background and synchronous API reads can correct a pre-existing poisoned record. |
| API read overlaps a newer genuine head/lifecycle event | Newer relevant state survives; the loser is not falsely verified and needed reconciliation survives. |
| Label traffic overlaps correction | A superseded read re-arms with a fresh HTTP start and no ETag; once the finite burst stops, ordinary repair converges. |
| Poisoned record with a saved ETag; in-flight 304 overlaps a webhook | No false verification; automatic validator migration and a full read correct the record. |
| Canonical PR, survey, local cache and ETag reads after success | Subsequent new reads agree on the correction; old-head dependent coverage is not reused as current. |
| Many simultaneous reports for one PR | Callers share a fetch on one instance, receive a committed result or retryable failure, and do not multiply inference or history reads. |
| Two different PRs; one refresh is slow | Independent work and webhook ingestion continue; no network wait holds the writer. |
| Database/publication failure after a successful API fetch | No success response or false verification; previous facts and automatic retry remain. |
| Client timeout before or after commit | Retry is safe; committed correction survives; no acknowledgement is required. |
| Service/worker restart, failed or hung shared promise, late response | No abandoned ownership or permanent exclusion; the next ordinary attempt recovers, and a late superseded response cannot overwrite a newer repair. |
| Quota exhaustion, secondary limit, installation capacity wait | Existing deadlines are honored; repeated callers do not move the deadline or create a polling storm. |
| Old server returns unsupported endpoint; auth or transport fails | Worker retains ordinary fallback/reselection and later recovery; no per-PR error charge or fleet-wide gate. |
| One refresh returns a distant retry deadline; two GitHub read paths repeatedly disagree | Only that PR/head waits; ordinary survey reads and unrelated work remain usable. Recent server validation avoids duplicate reads, and repeated disagreement uses a bounded slow retry. |
| Review command reports a mismatch | No review error, false completion, stuck issue, or general worker failure backoff; claims release and other work proceeds. |
| Existing exhausted stale-head error cap; mixed genuine errors; crash during refund | Proven stale attempts are refunded once, before suppression; genuine errors survive and an interrupted adjustment resumes automatically. |
| PR is closed, deleted, reopened, or changes again after repair | Existing lifecycle handling and final head checks remain correct; no stale candidate spins indefinitely. |
| Background history backlog with available quota | Targeted repair and normal useful work progress without waiting for the backlog to drain. |

Measure healthy-round overhead, request count, repair latency, publication time, ordinary GitHub call volume, useful worker completions, and repeat mismatches before and after. Background completion and webhook latency must also remain healthy. A reduction in the displayed failed-round count alone is not acceptance.

## Release sequence and completion

1. Land the ingestion, ordering, publication, and background-convergence fixes in TauCetiTools first. They must help existing workers independently of the new endpoint.
2. Add and validate the narrow synchronous endpoint using the same read and publication helpers. Test against realistic backlog and injected failures before production rollout.
3. Add the structured mismatch outcome in TauCetiReview and the optional refresh/reselection handling in TauCetiWorker. Test old/new combinations explicitly. Deploy server capability before clients that use it; keep unsupported-server handling functional.
4. Update the normal worker runtime pins and let ordinary workers encounter and repair affected PRs. Successful background repair may correct them first; do not require manually rewriting database rows, deleting ledgers, draining all jobs, resetting counters, or restarting the fleet to demonstrate recovery.
5. Observe real work and a controlled test-environment outage/recovery. Verify that affected PRs resume automatically and that useful throughput, webhook application, and background repair do not regress.

Keep changes reviewable in their owning upstream repositories and target their default branches. Separate implementation, deployment, and observed recovery in the completion report. Keep the ingestion/publication fixes separable from the endpoint so removing the new endpoint does not require reverting those fixes. Do not roll back to timestamp-poisoning behavior as the remedy for an endpoint problem. Worker rollback must not require removing stored records or service capability.

Completion requires correction to reach all scheduling read paths; fresh API results to repair poisoned timestamps; mismatches to stop consuming review budgets and worker backoff; formerly affected PRs to resume without intervention; and outages, restarts, and quota waits to recover using ordinary operation. Do not expand this work into replacing storage, redesigning all reconciliation, adding persistent classifications, or repairing unrelated review-ledger corruption.

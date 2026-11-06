# Admission inside the existing queue service

## Intended behavior

The already-running queue service owns normal PR intake as well as queue processing. When review or CI changes, it reads the current evidence, applies the existing admission policy, and records the result in its existing database. Eligible heads enter the queue directly. A quota wait leaves the PR pending in the same process; a service restart restores pending work from that database.

Move admission execution out of finite GitHub Actions jobs. Remove automatic workflow reruns, self-dispatch, delayed successor jobs, Actions-cache checkpoints, and admission progress check runs. Do not add a separate service or a queue-side checker that repairs another admission producer. Policy evaluation becomes the service's primary intake operation.

This is a larger implementation change than adjusting workflow limits, but gives admission the same process lifetime, persistent state, and restart behavior as the queue it feeds. The [chart repair](queue-readiness-observations.md) remains reporting only and can reuse the same PR observations.

## Established failure

Five consecutive November 5 auto-merge runs missed their progress cache, failed to save it, and stopped at an artificial work allowance. [Run 42129076980](https://github.com/TauCetiProject/TauCeti/actions/runs/42129076980) stopped at 104 `gh` invocations with 5,248 REST requests remaining. GitHub reported read-only runtime cache access and `cache write denied: token has no writable scopes`.

Without the checkpoint, every PR appeared fresh and newer activity displaced older ready work. Even with a checkpoint, unchanged ready PRs could be repeatedly evaluated. The 120/800 invocation ceilings, fixed reserves, and 30-call allowance compared subprocess counts with unlike REST/GraphQL resource units. Unfinished work did not have a reliable owner after the job stopped.

The queue already has a continuously running controller, authenticated GitHub webhooks, a GitHub client, an approval poll, SQLite state, a journal, and retryable side effects. Reuse those facilities instead of building a workflow handoff protocol. The existing service unit supplies process restart behavior; verify that it is actually installed and supervised on the deployment host rather than assuming a detached process will survive reboot.

## 1. Run the existing policy in normal intake

Factor the effective TauCetiReview admission policy into a reusable evaluator with explicit inputs and outputs. Reuse the actual implementation and fixtures; do not copy it into a second independently maintained policy. Keep reviewer execution and provider credentials outside this evaluator. Admission reads evidence and evaluates policy; it neither runs models nor builds candidate code.

The service loads a deliberate, trusted policy revision. Preserve all current review, scope, head/base, hold, and revocation rules. Before implementation, identify the effective production policy pin and its project configuration; do not assume similarly named workflow pins are equivalent. Policy changes remain explicit deployment/configuration changes, never executable code supplied by a PR.

Moving evaluation changes the execution and evidence boundary and must be represented explicitly. The review system continues to produce its normal review evidence; the queue's ability to write GitHub does not constitute review approval. Validate the same head-bound rubric results and reviewed diff that the existing policy requires. An internal decision records its head, reviewed base/diff, policy revision, and source evidence references. It must not pretend to be a check signed by the review App.

A successful eligible decision calls the existing controller admission operation with that evidence. Waiting/refused decisions remain normal intake results until a relevant input changes. Preserve explicit withdrawal and cancellation intent: a restart, repeated poll, changed incidental comment, or mere reevaluation cannot manufacture a new approval for an explicitly withdrawn head. Preserve existing rules for already-admitted membership rather than adding an exceptional eviction path.

Keep the identity of evaluated inputs separate from the identity of review authorization. CI, labels, body, and trusted policy/configuration changes can require reevaluation without constituting a new approval. Record withdrawal durably against the head and its actual authorization, including withdrawals during an in-flight evaluation and before the first service-owned evaluation. An incidental edit to an existing review, replayed event, or late legacy publication cannot supply fresh authorization. Restoring a withdrawn head requires fresh approval intent under the preserved admission rules.

## 2. Retain pending PRs in the existing database

Extend the controller's persistent work machinery with PR-keyed admission state: observed input version, last successful evaluation version/result, last attempt, and an actual retry deadline when needed. Keep pending policy work distinct from already-approved queue inputs. This is state in the existing service/database, not another scheduler or an external work queue.

Track the inputs the policy actually reads: PR/head and target identity, draft/hold/scope-relevant labels, relevant body and review/comment versions, effective build/bump/scope results, reviewed diff/base, and policy/configuration revision. A build status change must invalidate an evaluation even if PR `updated_at` does not change. Exclude the service's own output and bookkeeping timestamps so recording a result cannot create more work.

Authenticated, relevant webhooks mark the affected PR dirty and persist that fact before acknowledging delivery. Duplicate deliveries coalesce on the same pending record. Evaluation happens outside the webhook request, using fresh source reads. If an input changes during evaluation, committing the old result cannot acknowledge the newer version; recheck head/base and decision fences before admission and retain newer pending work.

A successful refusal/wait is a completed evaluation of those inputs. A request failure is not. Record attempts separately from success so failing PRs move behind other runnable work without being forgotten. Unchanged completed versions need no repeated expensive evaluation. There is no maximum lifetime attempt count or fixed number of PRs permitted to progress.

## 3. Reuse events and the existing discovery path

Extend the queue's existing webhook intake to observe the relevant review, PR, status, check, and base/configuration changes. Use its existing complete PR discovery at startup and for missed-delivery reconciliation; do not introduce a second repository scanner or a corrective full-sweep workflow.

Share one compact paginated PR snapshot between intake reconciliation and consumers such as the queue chart. Complete every page and validate nested connection completeness. An incomplete listing cannot imply that unseen work disappeared or that a previously approved PR is safe. Read missing decision dependencies through the existing policy adapter. Pagination is a transport detail, never a repository-size cutoff.

Process revocation-relevant changes promptly and fairly service the remaining pending PRs. A busy or failing PR cannot monopolize intake. Use the service's existing worker/loop infrastructure: network waits and policy evaluation must not hold the database/controller lock or block webhook acknowledgment, CI result handling, batch promotion, or status serving. There is one normal admission implementation, not a second loop checking whether it did its job.

On startup, restore pending records and their deadlines, then reconcile current source state. An already-running process automatically proceeds when a deadline arrives or new usable input appears. An actual process crash uses the ordinary service supervisor and the same startup behavior; it requires no fresh PR event or manually launched workflow.

## 4. Respect actual resources in place

Use the service's GitHub App and existing rate-aware client. There is no ephemeral workflow read token, new App, copied review App credential, or idle Actions runner. Reads and mutations use their actual REST/GraphQL buckets and current limits, reset times, and Retry-After deadlines. Reuse the client's credential refresh and error handling.

Remove unconditional admission stop rules based on subprocess count, PR count, fixed quota reserves, or fixed per-pass call allowances. Inspect the existing approval-poll reserve as well: a constant reserve must not become a permanent barrier to the new primary intake. Do not replace the old values with larger numbers or operator-tuned work caps.

When an affected bucket is unavailable, record its absolute next-eligible time and let other runnable service work proceed. Repeated attempts cannot extend a known server deadline indefinitely, and one blocked PR cannot suspend unrelated work whose resources are available. Account for essential CI/queue mutations and revocations in the same client's scheduling rather than letting background discovery consume every opportunity first.

Short retry bursts may end, but the PR remains pending for its later deadline. A failed request is never a policy refusal or an inferred zero. Resource waits do not terminate the process, dispatch another job, or restart a workflow. When capacity returns, this same intake path proceeds.

## 5. Keep one engine during inference outages

Admission policy evaluation is deterministic and needs no inference. Existing valid review evidence can still be evaluated during a provider outage; work needing a new review waits for that evidence. Never bypass review requirements to compensate for unavailable inference.

Until inference-free operation is implemented, keep the existing queue's inference-hold behavior for work that requires agent repair: preserve pending tasks, membership, retained source, and diagnostics; respect the existing sealing/hold rules; retry eligible provider/worker paths and resume automatically when inference returns. The process, intake state, status, and database remain operational. Do not turn an outage into a terminal refusal or require an Actions rerun.

Retire automatic failover to the legacy Bors engine for this repository. Such failover would require a second complete admission implementation plus migration of waiting inputs, sealed heads, repair tasks, cancellation intent, and promotion ownership during an outage. That conflicts with one authoritative intake/queue owner and introduces another failure-prone handoff. It is not part of runtime recovery.

Include that retirement in implementation and cutover: inventory and remove any automatic Bors-fallback routing or engine-switch hooks, update affected callers/configuration/documentation, and verify inference failure cannot transfer ownership back to Bors.

The queue must also be able to operate without any inference by using splitting within this same engine. That requirement is recorded as a [TODO in the queue specification](../redesign/specs/ai-merge-queue.md#inference-unavailability); its design is deferred. Hold-and-retry is the interim behavior, not the final inference-free operating strategy. Do not design splitting as part of this admission plan.

Keep necessary Bors-compatible commands, feed schemas, hostnames, and configuration names as compatibility surfaces; they do not imply that the legacy engine owns work. The inspected `MERGE_BACKEND=bors` still selects this compatibility protocol for the replacement service. Inventory actual producers and consumers before retiring anything, and scope retirement to this repository rather than other Bors users. A deliberate deployment rollback, if retained, is a separately documented operator procedure with explicit state ownership, not an inference fallback.

## 6. Cut over to one owner and retire the old execution path

Inventory every automatic producer of eligibility decisions. Keep review-score publication and normal CI enabled, but retire their separate Actions admission/merge-sweep execution once the service owns intake. Do not leave a scheduled workflow as a permanently necessary repair path.

Install the reusable policy and durable intake state first, and compare computed results against captured production-shaped fixtures and the existing policy before enabling side effects. Use a deliberate owner/mode switch for cutover. The service's primary intake must be active before the old path becomes unnecessary; there must be exactly one authoritative automatic admission owner after the switch.

At that boundary, handle in-flight old eligibility publications explicitly. Late output from an old job cannot create fresh approval intent after a manual cancellation or compete with the new owner. Preserve valid already-approved inputs, existing batch membership, operator commands, and their evidence. Stop treating legacy automated publications as a second active policy producer. Preserve the source review evidence that the new evaluator still needs.

Deploy live with worker and CI admissions enabled. Verify the running policy/service revisions, actual process supervision, persistent database/backup location, pending PR progress, useful admissions/landings, and interrupted work. No fleet drain, mass rebuild, Actions self-dispatch, or recurring operator action is part of completion.

## Focused verification

Extend the existing queue and shared-policy fixtures:

- The reusable evaluator agrees with the existing policy on approvals, waits, unsafe reviews, scope, holds, changed heads/bases, and policy changes. It executes no PR code or model calls.
- Relevant review/CI/input changes create pending work; duplicate events coalesce. A newer event arriving during evaluation remains pending after an older result.
- Unchanged evaluated inputs create no work. Refusals do not loop; transient failures remain retryable; old ready PRs and revocations receive service under continuing traffic and poison PRs.
- Resource availability permits progress beyond all former work caps. Real quota exhaustion retains PRs and resumes in the same process at the actual deadline without launching any workflow.
- A blocked API bucket or slow policy read does not hold the controller lock, stall CI/promotion, or prevent status and webhook handling. Essential mutations retain service under background intake load.
- Kill before and after an admission state update or ambiguous GitHub side effect. Restart from the existing database, observe prior effects, and recover without duplicate inputs or new approval intent.
- Repeated evaluation, replay, restart, CI/configuration changes, incidental review edits, and late legacy checks cannot re-admit an explicitly withdrawn/cancelled head. Exercise withdrawal during evaluation and before the first service-owned evaluation. Preserve already-admitted membership rules.
- Use repository sizes spanning multiple API pages and complete discovery failures. Missing evidence cannot discard pending work or become proof of readiness.
- Exercise the owner switch with ordinary live work and in-flight legacy jobs. After cutover, no admission workflow reruns, progress-cache dependency, or corrective queue-side scanner remains necessary.
- With every inference path unavailable, deterministic intake still evaluates existing evidence, new-review work remains pending, and repair-dependent work retains its state under the existing hold rules. Restore inference and verify automatic recovery without an engine switch, lost membership, bypassed review, or operator action.

Completion means the service owns intake end to end: pending work persists, actual capacity is used, temporary failures recover in place, and review/merge safeguards remain intact. Process restarts remain ordinary fault recovery; normal work never depends on splitting admission across finite workflow runs.

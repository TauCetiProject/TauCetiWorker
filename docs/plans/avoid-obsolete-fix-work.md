# Avoid obsolete and repeated fix work

Restore the distinction between discovering possible work and deciding what repair is actually needed. Preserve the waterfall, autonomous recovery, and useful throughput. Correct the existing read and outcome paths before considering additional coordination mechanisms.

## Findings

The October 13 mirror integration ([#250](https://github.com/TauCetiProject/TauCetiWorker/pull/250)) changed previously live `pr_view()` and progress reads into potentially stale mirror reads. A subsequent change ([#256](https://github.com/TauCetiProject/TauCetiWorker/pull/256)) made only the post-work progress read direct. The resulting comparison can credit an earlier worker's push to a later, unchanged attempt.

Partial-history maintenance ([#379](https://github.com/TauCetiProject/TauCetiWorker/pull/379)) allows useful investigation when the mirror lacks comments, but relies on a current-head check that still accepts mirrored data. Ordinary `fix` also lacks the checkout head comparison already used by rebase and CI repairs. Consequently, it can prepare caches and launch a model after checkout has already exposed that its assignment is obsolete.

Branch claims coordinate active ownership; they do not remember completed repairs. Per-worker attempt counters multiply retries across workers. These amplify stale selection but do not justify introducing a mandatory shared retry ledger.

Observed examples include 21 successful fix events naming the same obsolete head of #22817 and an unchanged paid session for #22870. A reproduction using deployed worker functions and fake reads counted progress with zero mutations. Historical records show substantial repeated-head activity on October 20, but those counts alone do not establish how many historical rounds were unnecessary.

## Implementation

1. **Make the selected repair use current facts.** Keep broad surveys mirrored. Once the existing branch claim is acquired, reconcile only the selected PR against GitHub, explicitly bypassing both mirror and local read caches. Reuse that result for head, lifecycle, pause labels, and the round's baseline rather than performing several redundant header reads. Audit `force=True` callers: bypassing a local cache must not be mistaken for bypassing the mirror.

   Use current review and author-reply evidence to avoid repeating a finding already repaired or awaiting re-review. Preserve the existing ability to investigate incomplete histories; do not require complete mirror coverage before useful work can proceed. Apply live facts to the current round's candidate view so it does not keep retrying an obsolete candidate within that pass. Do not introduce a persistent suppression list or age-based eligibility rule.

2. **Stop obsolete assignments before expensive preparation.** Extend the existing comparison between the selected commit and the commit actually checked out to ordinary `fix`. Perform it immediately after checkout, before cache warming or inference. A known mismatch means the assignment changed: release its claim, restore any attempt increment for work that never launched, and immediately try other work in the same waterfall. Use the existing decline path; do not turn this into a failed round, sleep, or dependency wait. Never undo a genuinely launched attempt merely because it later made no changes.

   The checkout comparison is also a cheap backstop for a head change during preparation. Preserve existing outage fallbacks when optional reads are unavailable; this change must not make a new telemetry or history read an admission prerequisite.

3. **Fix progress attribution.** Establish the baseline from the current state used to launch the repair. Never compare an old mirrored baseline with a current direct result. Reuse existing per-round scoped publication markers and GitHub mutation receipts to identify this round's pushes, replies, or closure. A peer's earlier push and an already-up-to-date push are not new progress by this round.

   Keep legitimate comment-only contests and confirmed closures as useful outcomes. Missing or unreadable evidence is an unknown observation, not proof of success or failure. Represent that distinction in existing reporting without blocking launch, losing source, or requiring a durable accounting write. Do not interpret model prose to decide whether a repair succeeded.

4. **Keep coordination small.** Retain the existing branch claims, heartbeat, branch compare-and-swap publication, and retry policies. The current owner should inspect current work before spending; its successor should see the updated PR through the same selected-PR reconciliation. First measure what repetition remains after these corrections. Do not add a completion service, mandatory completion receipt, shared hard retry cap, new timeout, or fleet-wide wait. Repair mirror freshness in the separately owned mirror work; worker correctness must tolerate delayed webhooks.

5. **Make the existing counters honest.** Count a changed branch publication only when the existing Git push result says it changed the remote; the current `branch_push` event also fires for already-up-to-date pushes. Keep finished attempts distinct from confirmed useful outcomes. Add only the compact diagnostics needed to identify selected head, checked-out head, stale-candidate skips, and confirmed publication for the existing round. Reporting failures must remain nonblocking.

## Focused validation

Extend existing tests rather than creating another framework or a broad matrix:

- Reproduce an old mirrored head plus a newer GitHub head. The worker must select other work without warming caches or invoking a model, including when the head changes after the initial read.
- Exercise the claimed-PR path with stale review data and a current author reply. Confirm resolved work falls through, while genuinely incomplete history can still be investigated.
- Verify an unchanged round cannot acquire progress from the mixed-source comparison or an already-up-to-date push. Preserve a real changed push, a legitimate reply, and closure as outcomes. Unavailable reporting must not stop useful work.
- Reuse claim contention and failure tests to confirm ordinary contention continues to other candidates and optional bookkeeping failures introduce no new wait.

## Rollout and completion

Record a short baseline from existing round journals and publication evidence, then deploy through the normal Worker/Fleet pin flow. Observe a fixed cohort of affected PRs across several natural rounds, including #22817 and #22870 if still open. Measure paid launches against already-superseded heads, cache preparation avoided, actual changed publications, and whether workers continue through the waterfall. Separate legitimate repairs after new findings or new heads from repetitions of the same obsolete assignment.

Completion means known-obsolete repairs consume no model calls or cache preparation, earlier publications are not credited to unchanged rounds, and useful repairs, reviews, and authoring continue automatically through ordinary mirror delays. Do not infer success from finished-round or raw push-event counts alone. If selected-PR reads materially increase GitHub quota use, remove duplicate reads before adding any caching or coordination mechanism.

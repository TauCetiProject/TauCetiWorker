# Restore reliable fleet activity reporting

Restore useful PR state, landing and LOC measurements with small changes tied to measured failures: promote missing current-head files, retire obsolete jobs, remove the Bors receipt byte limit, and eliminate repeated worker-scope lookups. Existing supervisors and collectors must recover automatically after transient failures and restarts. Measure again before expanding the accounting or scheduler changes.

This is an implementation plan. The investigation made no production changes.

## Design constraints

- Use the existing shared-state scheduler, Bors journal and activity collector. Add no service, daemon, queue, resumable feed, operating mode or feature flag.
- Add no arbitrary payload limit, history cutoff, retry cap, job-age deadline or admission gate. Preserve existing resource and funding controls. Existing batching may divide work across ordinary passes; it must not discard useful work or require an operator to continue it.
- Decide whether work is needed from resource identity, current lifecycle and actual consumer demand. Job age and a dirty flag alone do not establish usefulness.
- Preserve stored facts and financial history. Removing an obsolete collection job does not delete its cached data, source journal or recovery evidence.
- Reporting failure must not stop workers, publish fabricated zeros or start a GitHub fallback storm. Missing evidence stays explicitly missing; normal background work retries automatically.
- Avoid new failure modes: no reporting dependency in worker admission, no operator acknowledgement to resume collection, and no second source of truth whose disagreement requires manual repair.

## Evidence and scope

The October 31 investigation found:

| Observation | Implication |
| --- | --- |
| 35,736 collection jobs; 34,526 overdue; 552 open PRs | Queue size greatly exceeds current work. |
| 28,848 jobs associated with closed or merged PRs | Historical collection is dominating the queue. This does not imply every historical file collection is dispensable. |
| Approximately 7,300 historical jobs already clean, complete and without recent demand | Retirement is delayed behind ordinary scheduling. |
| No duplicate operation-and-argument jobs | Fix relevance and redundant collection, rather than adding another deduplication system. |
| All 321 sampled host-authored PRs created in the preceding day lacked file collections | Missing LOC begins with collection starvation. |
| 10.58 MB Bors response rejected at 8 MiB | A local reader limit interrupts all landing imports. |
| Last successful Bors import predates the oldest candidate in the current response | Removing the byte limit restores current imports but cannot alone recover all missed history. |
| Approximately 53,400 unique paid invocations since October 18, including 44,000 review calls | The accounting population is real history; reviews often launch ten separate rubric calls. |
| 36 of 40 collector samples in worker-scope lookup | Repeated identical lookups are a demonstrated local bottleneck. |
| Shared service configured only for TauCeti; roadmap requests return 403 | Roadmap evidence cannot recover through retries alone. |

Local investigation artifacts, including the deployed service bundle and read-only database results, are in `/home/kim/tmp/loc-not-measured-20261031/`. Counts are snapshots, not permanent acceptance thresholds.

## 1. Restore useful collection and retire obsolete work

Implementation belongs in TauCetiTools' existing shared GitHub-state service.

### Promote current-head files directly

Add `files` to `requestOpenCoverage`, alongside the status and comment collections it already promotes. Include both missing file collections and dirty file jobs for open PRs. A previous head's collection can still have `complete=1`; that flag must not suppress collection for the new head.

Update the existing enqueue helper's complete-collection shortcut and lane decision as necessary so this promotion actually reaches the existing requested lane, including when old coverage is marked complete. Preserve active leases, generations and upstream retry deadlines; promotion must not reset a wait on every maintenance pass. Do not make reporting reads enqueue work or add another lane or scheduler.

Ship this direct LOC fix with the immediate small changes. It must not wait for the historical backlog to drain.

### Make the existing retirement sweep useful

Run the existing `retireQuotaObsolete` maintenance sweep without requiring a quota restriction. Extend its existing selection/deletion checks to retire the following work when the existing demand window has no recent consumer:

- Comment, review, thread and reaction jobs for terminal PRs, including dirty or incomplete jobs whose missing history is no longer needed by a consumer.
- Status jobs when the commit is no longer the current head of any mapped open PR, and unmapped status jobs without consumer demand. Preserve demanded historical reads.
- Completed final-head file jobs for terminal PRs. Keep missing or stale final-head files until the existing LOC validator accepts the snapshot, then retire their jobs.

Terminal PR detail must retain the head, base, file totals and merge facts needed by LOC and landing reporting. Accepted PR webhooks already write those fields into the stored resource, preserving earlier fields for the same head/base. Retire an otherwise unnecessary detail job when those facts are sufficient; fetch missing final detail once when they are not. A dirty flag caused only by unknown mergeability does not justify repeatedly querying a closed PR. Preserve uncertain or explicitly demanded resources.

Keep one consistent retirement decision between the sweep and its deletion helper. The current sweep selects the first group of keys; relaxing selection but leaving deletion stricter can repeatedly select an undeletable prefix. Test that ordinary maintenance reaches later eligible jobs when early candidates are retained, leased or changed concurrently. Use existing maintenance batching and generation/lease guards; add no new cleanup cursor service or manual drain requirement.

Keep the existing five-minute demand window and reopening behavior. A new relevant request or reopening must enqueue needed work automatically. Retirement removes jobs, not stored facts. Jobs legitimately recreated by later webhooks must retire again rather than accumulate.

### Remove one demonstrated source of unnecessary jobs

Delete the two eager reaction-job pushes after review-comment and review-thread collection. Current worker reaction consumers already read directly under `fresh=True, direct=True`; preserve those claim reads and their failure behavior. Explicit shared-service reaction requests can continue through normal demand handling.

Defer changes to thread/flat-comment collection reuse: the code already materializes flat comments from threads, and further fetch reduction has not been measured as a cause of this incident. Do not turn this repair into an audit of every collection producer. Measure necessary file completions and remaining queue composition before changing any more scheduling machinery.

## 2. Restore Bors imports with a straightforward snapshot

### Immediate reader fix

Remove the importer's 8 MiB rejection. Use ordinary response JSON decoding and remove the custom chunk accumulation and second buffer copy where possible. Do not replace 8 MiB with another fixed application byte ceiling. Retain normal authentication, transport error handling and validation of exact PR heads, base commits, construction events and successful main promotions.

A failed or interrupted read keeps previously imported facts. The existing cron retries on its normal schedule; successful imports remain idempotent. Ship this independently so it does not wait for queue or accounting changes.

### Complete recovery without a resumable feed

Add a normal promotions-only query filter to the existing candidates endpoint, alongside its existing `base` and `batch_id` filters. For this filtered request, return all candidates with promotion events available in the journal, retaining the existing envelope and `candidates` array shape. Include just the snapshot contract, base/base SHA, member PR/head identities, and construction/promotion events needed by the existing validator. Do not include admissions or unrelated assembly diagnostics in the filtered response. Keep the default diagnostic response unchanged, and preserve explicit unknowns for history predating the journal.

Point the importer at that filtered URL. Its existing parser and idempotent identity handling should continue to work. No cursor, acknowledgement protocol, checkpoint service, new endpoint or historical/live operating modes are required. Use the journal's existing indexed candidate/event relationships to obtain promotions; add an ordinary database index only if the query plan requires it.

This removes reliance on the rolling latest-100 diagnostic window. Deployment automatically recovers missed promotions, and the same path handles a later prolonged outage or a new installation. Do not add a replacement fixed history limit. Historical evidence grows with actual promotions, rather than with every construction attempt, repeated status observation and diagnostic payload.

The current diagnostic payload contains about 4.88 MB of candidate snapshots, 5.22 MB of events and 0.46 MB of admissions. Do not append complete history to that payload or make diagnostic readers download it. Keep the journal and its ordinary diagnostic response; neither needs redesigning or deleting for this repair.

An older producer ignores an unknown filter and returns its usual recent diagnostics, so changing the reader URL need not break current imports once the byte cap is removed. That compatibility does not establish historical recovery: deploy and verify the filtered producer, including a promotion outside the recent window, before declaring history complete. Missing or invalid evidence remains visible and retries automatically.

Measure complete-snapshot fetch, decode and upsert cost under the existing transport timeout and runtime memory budget. Do not lengthen timeouts or add another limit speculatively. If real measurements later show full-history work becoming material, evaluate using the existing last-success timestamp with an overlap as an ordinary query filter. That is deferred; no new checkpoint or resume protocol is part of this plan.

## 3. Remove repeated accounting lookups, then measure

Implementation belongs in [activity_costs.py](../../tauceti_worker/activity_costs.py) and [activity.py](../../tauceti_worker/activity.py).

### First remove the measured bottleneck

Memoize worker-to-scope resolution for one preparation pass and reuse the resolved scope in every row for that invocation. Delete the second identical lookup. There should be one latest-round lookup per distinct worker, independent of invocation count. Consider an index on the actual `(worker, started)` access pattern only if the remaining lookups still matter after memoization.

Keep the existing scope-assignment semantics in this change. Historical worker reassignment is a separate correctness question and does not justify a new scope registry for this repair.

### Establish what, if anything, remains expensive

After the scope fix, measure one normal preparation pass by phase: ledger read/parse, replay, cached usage lookup, allocation, table writes and summaries. Include peak process memory because the collector runs inside the worker manager. Use existing profiling or temporary measurement, not a new telemetry service. Check both preparation freshness and budget/worker responsiveness.

Stop if preparation keeps up with its existing schedule and resource use is acceptable. Full recomputation of a modest derived report is preferable to introducing dependency tracking that measurements do not justify. Preserve the authoritative ledger, current costing semantics and automatic ordinary retries.

### Defer accounting architecture changes

Do not make incremental accounting or reuse of the budget's `projection.sqlite` an implementation or release requirement. That projection uses SQLite rollback journaling and participates in admission. A long reporting read could block its writers or provoke a projection rebuild under the budget lock. It also lacks the first-settlement record timestamp currently used by the report. Changing that ownership/schema is outside the measured scope-lookup fix.

Per-invocation signatures are not sufficient to cache cost allocation safely: the existing start-ordered pass deduplicates provider messages across invocations, and scope depends on the worker's latest round. A late correction or changed earlier invocation can alter another contribution. Preserve that global calculation rather than adding a dependency graph.

If measurement shows table writes dominate, first compare fully recomputed rows with stored rows and write only differences, including removal of obsolete rows. If parsing/replay or memory dominates, prepare a separate narrowly measured proposal. Do not truncate financial history, change review rubric counts or introduce another accounting database as part of this repair.

## 4. Give roadmap PRs a working evidence source

Add TauCetiRoadmap to the existing shared service's repository configuration, read-only GitHub App installation and relevant client scope. Expanding the App's selected repositories may require a one-time organization-admin action if it is not already installed there; this is setup, not a recurring runtime acknowledgement. Initialize its stored state through the existing bootstrap/ingestion path. Preserve repository separation and existing installation scheduling.

Then let the normal activity collector refresh already tracked roadmap PRs. Do not add a reporting-specific direct-GitHub fallback, another collector or a second authentication scheme. Use ordinary GitHub merge evidence for roadmap PRs; do not require Bors evidence for a repository that does not use Bors. A non-Lean PR can correctly have measured zero Lean LOC.

## Verification

Extend focused tests in the owning repositories; use captured shapes and synthetic fixtures rather than paid agent calls or artificial CI jobs.

- **Current files:** promote missing and dirty file jobs for open PRs, including an old `complete=1` collection after a head change. Verify the enqueue helper does not skip them or leave them stranded in the active lane, and repeated promotion respects existing leases and retry deadlines.
- **Job relevance:** open to closed, head replacement, reopening and new explicit demand. Include dirty/incomplete obsolete jobs, sufficient versus missing final PR detail, retained final-head files, an unmapped demanded resource and concurrent generation changes. Verify later eligible jobs retire despite retained early keys, and cached historical facts survive retirement.
- **Reaction fan-out:** historical comments and threads do not create eager reaction jobs. Ordinary direct claim reads and explicitly demanded shared reads retain their existing behavior.
- **Bors:** accept a valid response larger than the former limit; recover promotions older than the recent diagnostic window and across a restart; reject mismatched construction/base/head evidence; repeated complete snapshots do not duplicate landings. Follow a failed read with a successful ordinary cron pass and verify recovery.
- **Accounting:** a large history performs scope resolution per worker and preserves totals. Include provider messages shared across invocations, settlement, correction, stopped usage and replacement-ledger cases in the existing correctness tests. Measure phase timings and memory after the small fix; no incremental-cache implementation is needed to pass acceptance.
- **Roadmap:** authenticate, bootstrap, ingest and read actual tracked observations. Confirm merge state and measured zero LOC where appropriate.

Keep expensive collection and cost preparation out of `status`. Preserve the current report layout and explicit observation errors and missing measurements.

## Release order and acceptance

Prepare focused upstream changes in their owning repositories: TauCetiTools for collection and imports, Bors for the promotion filter, Worker for scope lookup, and the normal Fleet runtime pin updates where needed. Target each repository's default branch. Independent fixes may ship independently; no fix waits for all other work to finish.

1. Ship the small direct fixes: remove the Bors byte cap; promote missing/dirty current files; stop eager reaction fan-out; run the existing retirement sweep without its quota-only condition; memoize scope resolution and remove the duplicate lookup.
2. Measure file completions, LOC coverage, import success and fetch cost, and activity phase timings/memory. Do not make these fixes wait for an accounting rewrite or historical queue drain.
3. Extend the single retirement decision and sweep to remove the specified obsolete dirty/incomplete jobs, retaining required final detail/files and concurrent-demand guards.
4. Deploy the compact promotion filter and switch the reader URL. Verify automatic recovery outside the recent window before claiming historical completeness. Configure roadmap coverage through the existing setup paths.
5. Observe ordinary production work and outage recovery. Do not manually drain jobs or restart workers merely to manufacture a clean measurement. Further accounting or scheduler optimization requires a measured remaining bottleneck.

Use the same production measurements before and after: job populations by relevance, oldest needed collection, completed current-head file collections, PR/LOC coverage, confirmed landing imports, activity preparation duration and age, collector CPU, and API calls spent on obsolete history. Also check useful worker completions and CI to ensure reporting work does not displace them.

Completion requires obsolete jobs to retire without accumulating again or needing intervention; new and existing useful PRs to gain current file/LOC observations; historical and new Bors promotions to appear automatically; roadmap observations to work; and activity preparation to keep up with its existing minute cadence. Existing stale warnings provide the reporting-freshness criterion; add no new shutdown threshold. A complete incremental accounting implementation is not an acceptance requirement.

Report implementation, deployment and observed recovery separately. A smaller queue or a merged PR alone does not establish recovery. If an old release would recreate obsolete jobs or mishandle complete history, record that rollback constraint and ship the smallest correction through the same release path. Preserve the source journals and cached facts throughout.

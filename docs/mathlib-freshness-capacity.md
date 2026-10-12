# Mathlib freshness and automation capacity

The three-day freshness target requires a reliable merge queue, a dedicated
daily dependency update, and enough CI capacity to finish repairs before the
deadline. Recovering stranded approvals removes one failure mode. It does not
make the existing system safe to scale by simply adding workers. Shared GitHub
reads address duplicated API traffic; repository-wide CI and cache transfer
still grow with repository size, and overloaded queues make update latency
grow much faster than useful throughput.

This assessment uses measurements from October 12, 2026. Recommendations below
are separate from the changes already deployed. The
[measurement snapshot](evidence/freshness-capacity-2026-10-12.json) preserves
the source definition, CI decomposition, API window, and scaling assumptions;
the [corrected cost report](evidence/cost-report-2026-10-12.txt) preserves the
exact-model output evaluated at the measured source size.

## Freshness failure and recovery

At 20:51 UTC, main at
[`a65deade`](https://github.com/TauCetiProject/TauCeti/commit/a65deaded1cae2fdc20c06b7cd5fef04c8e80d6c)
still pinned Mathlib
[`0493595e`](https://github.com/leanprover-community/mathlib4/commit/0493595ecd5da8ef14742febab99728f98e948e8),
whose committer timestamp is September 29 at 11:56:28 UTC. Its age was more
than thirteen days. Changing a manifest, or retrying a bump of the same old
commit, cannot make that pin fresh. The stale-pin detector now compares the
actual pinned commit's age against 72 hours, and fails when it cannot establish
that age: [TauCeti PR 15170](https://github.com/TauCetiProject/TauCeti/pull/15170).

The expensive Quasicoherent proof was a genuine compatibility-repair obstacle.
Its refactor reduced the observed local compilation from about 78 seconds and
10.3 GiB to 3–7 seconds and about 1.4 GiB, and landed through Bors:
[TauCeti PR 15207](https://github.com/TauCetiProject/TauCeti/pull/15207).
That result repairs this proof; a complete build against a fresh Mathlib pin
still has to establish compatibility across the rest of TauCeti.

The old Bors recovery path could delete retry membership without preserving
enough intent to re-admit the PR. Successful standalone CI and reviews then
left some PRs without a queue owner. This required fixing Bors persistence,
rather than launching an AI session to push unchanged branches. The deployed
[Bors PR 11](https://github.com/TauCetiProject/bors-ng/pull/11) preserves
exact-head approval intent, resumes interrupted batches, and retries transient
staging or promotion failures with backoff. It reuses the native queue.
Recovery processes a bounded number of candidates per tick and honors
withdrawals, holds, head changes, and genuine individual integration failures.

At the initial 09:29 UTC verification, the audited legacy bootstrap had recorded
56 approvals and automatically readmitted 53 PRs. Three subsequent withdrawals
or review changes stopped re-admission; a repeated bootstrap changed no rows,
and the original 133 active memberships remained represented. Later repaired
heads can obtain fresh approvals through the normal gate. The canary,
[PR 14684](https://github.com/TauCetiProject/TauCeti/pull/14684), survived failed
integration attempts and remained in retry batch 562 at 20:55 UTC. A successful
canary landing remains necessary to finish the production pilot. A failed
batch is not evidence that every member has a bad branch: the latest failed
[integration run](https://github.com/TauCetiProject/TauCeti/actions/runs/38288099360)
reported a ProjectiveLine identifier mismatch, not a failure of every PR in
the batch.

Workers should edit only a genuine GitHub `CONFLICTING` head or an authenticated
repair handoff for that exact head. Batch-level failure or a speculative label
alone must not trigger a rebase. Bors owns unchanged-head requeuing; workers own
source repair. The user's `bors merge` scope-approval mechanism also keeps
infrastructure changes in the queue instead of invalidating integration runs
with direct merges.

[Worker PR 247](https://github.com/TauCetiProject/TauCetiWorker/pull/247)
renews the conflict-rebase budget after observing a different head that is
build-green, mergeable, and free of repair or hold labels. A push, a CI rerun,
or a repeated handoff cannot renew it. Candidates retain the selected budget
key through execution and infrastructure refunds. This adds no GitHub request
or AI session. The bound remains per worker, using the existing claim mechanism;
it is not a new fleet-wide spending limit.

## API usage

The pre-service four-hour sample ending at 05:56 UTC covered nine science
workers. It recorded 18,944 REST attempts, or 4,736/hour across the fleet;
3,045 GraphQL HTTP attempts, or 761/hour; and 2,508 explicitly recorded
GraphQL points, or 627/hour. The point count is incomplete for native CLI
requests, and direct HTTP/controller probes are outside this instrumentation.
Almost all recorded REST attempts were issue or review-comment reads.

Replicating the same polling behavior at ten times the worker count would
produce about 47,360 REST attempts/hour, even if useful work did not increase.
Adding personal tokens for the same user does not create independent quota.
GitHub documents separate REST and GraphQL primary pools, with installation
quotas and additional secondary limits:
[REST limits](https://docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api),
[GraphQL limits](https://docs.github.com/en/graphql/overview/rate-limits-and-query-limits-for-the-graphql-api).

The shared GitHub state service instead materializes repository state once,
updates it from webhooks, and performs bounded reconciliation. All nine workers
had applied its read policy by 11:27 UTC. Larger independent-read batches were
measured at one point for 100 conversation histories, statuses, or reaction
sets; dense histories still require pagination. Its production App reported
separate 5,600/hour REST and GraphQL pools.

A conservative modeled workload with 5,000 open PRs, 800 recently demanded
PRs, 200 changes/hour, and 20 reaction-bearing comments per hot PR uses about
3,273 REST requests and 3,783 GraphQL points/hour. These estimates fit the
observed primary limits, but they are not a production load test. Dense nested
histories, recovery bursts, fallback reads, storage/compute costs, and secondary
write limits need separate measurements. The service capacity evidence and
query builders are owned by the service implementation; do not treat the
pre-service sample as its post-rollout performance.

Use one durable read service with pagination and recovery checkpoints. Keep
normal worker requests from multiplying audits; coalesce repairs for the same
object. Respect real throttling responses and retain unpublished writes for
retry. Add alerts for fallback volume, incomplete coverage, oldest pending
repair, and quota refusals. A read cache cannot remove write traffic or supply
unlimited capacity for new PRs and review comments.

## Current size and CI scaling

The measured source snapshot contains **2,842,170 lines in 13,430 Lean files**,
including comments and blank lines in tracked `TauCeti/**/*.lean` and
`TauCeti.lean`. It excludes dependencies and build artifacts. Its source bytes
total 143,787,421. The commit and measurement time are above; regenerate the
count from a Git archive of that commit rather than a dirty worker checkout.

The corrected cost report evaluates its CI model at that measured size:
52.97 runner-minutes, 5.61 vCPU-hours, and $0.93 equivalent compute per merged
PR. The proxy decomposition is 11.49 fixed runner-minutes, 24.76 touched-code
runner-minutes, and 16.72 repository-wide runner-minutes. These are model
estimates from historical job classifications, not a measurement of today's
end-to-end queue latency. Historical operating changes prevent a reliable
causal fit of runtime against repository size.

For the following stress scenario, keep PR size, retry frequency, hardware,
cache behavior, and the fixed/touched-code work constant. Scale only the
repository-wide component linearly with existing source size. Cache transfer
assumes the same number of full-cache fetches per PR and constant bytes per
line. Arrival rate refers to completed-work demand, not worker count.

| Arrival rate | Repository size | Runner-minutes per merged PR | Aggregate runner capacity relative to current | Full-cache transfer relative to current |
| --- | --- | ---: | ---: | ---: |
| Current | Current | 52.97 | 1× | 1× |
| 10× | Current | 52.97 | 10× | 10× |
| Current | 100× | 1,707.85 | 32.24× | 100× |
| 10× | 100× | 1,707.85 | 322.43× | 1,000× |

Runner-minutes measure total work; parallel jobs can reduce elapsed time without
reducing that work. A model that scales all infrastructure with size would
require 1,000× capacity in the combined case. Neither scenario includes
superlinear elaboration, dependency fan-out changes, memory pressure, longer
histories, or extra retries. They expose the design risk rather than certify
capacity at 100× size.

Repository-wide work once per merge gives cumulative quadratic work as the
repository grows: integrating a per-merge cost proportional to existing size
produces a term proportional to size squared. Compilation can also become
nonlinear locally, as the Quasicoherent incident demonstrated. Faster model
generation does not remove either effect.

Move full-repository work off each small PR when a sound incremental check is
available. Build changed modules and their affected dependency closure; retain
full integration checks and scheduled full audits for the properties that
require them. Fetch content-addressed artifacts for the required closure
instead of repeatedly transferring a complete cache. Separate aggregate
compute, wall time, transferred bytes, peak memory, and cold-cache time in
telemetry. Large serial batch preparation and per-PR post-merge bookkeeping
also need bounded processing with durable checkpoints so they cannot prevent
ordinary queue reconciliation for long periods.

## Model costs and useful throughput

[Worker PR 245](https://github.com/TauCetiProject/TauCetiWorker/pull/245)
landed the exact-model reporting and current-size CI rendering changes. The
8,540-transcript sample includes the following API-equivalent costs imputed
from measured tokens, using the report's recorded price assumptions:

| Exact model | Preparation sessions | Preparation median / mean | Revision sessions | Revision median / mean |
| --- | ---: | --- | ---: | --- |
| `claude-opus-5` | 404 | $10.80 / $11.79 | 1,642 | $1.89 / $2.44 |
| `claude-opus-5-5` | 570 | $6.15 / $7.17 | 847 | $0.51 / $0.68 |
| `gpt-6.1-sol` | 490 | $1.42 / $1.33 | 529 | $0.36 / $0.39 |

These are observational phase samples. They do not establish equivalent
quality, tasks, or success rates across models, and API-equivalent cost is not
the subscription invoice. The revision-rate sample is a local lower bound.
The report does not invent a complete `gpt-6.1-sol` cost per PR when exact-model
review-cost coverage is missing.

The observed mixed-model scenario is $17.35 per PR: $7.01 preparation, $6.39
reviews, and $3.94 revisions. A complete `claude-opus-5-5` scenario using shared
review/revision rates is $13.33 per PR. This comparison is conditional on those
rates and measured phase means. Measure accepted, merged work per dollar and
per CI-hour on comparable tasks before changing default models. Include
rejected work, review retries, and compatibility repairs in that denominator.

## Keeping the pin below three days

Use the existing bump workflow and workers, with these operating requirements:

1. Select a recent upstream commit at least daily. Record the pinned SHA and
   its upstream committer timestamp. Measure the SLO continuously as
   `now - pinned_commit_timestamp`, independent of manifest modification time.
2. Make the bump a prioritized single-PR integration unit and reserve runner
   capacity for it. A compatibility repair must not wait behind a giant
   speculative batch. The daily selection cadence and the selected commit's
   age consume part of the 72-hour budget; the remainder must cover repair,
   review, cold integration CI, and promotion.
3. Alert before the deadline, for example at 36 and 48 hours, and identify the
   current blocking stage and owner. Escalate a genuine compilation or
   upstream compatibility failure promptly; retrying queue admission is not
   a proof repair. Any upstream change that cannot be repaired in time is an
   explicit SLO violation, rather than a reason to reset the clock.
4. Maintain exact-head queue intent, bounded retries with backoff, and durable
   withdrawals. Do not synthesize pushes or AI editing sessions merely to
   wake the queue. Keep source-repair attempts bounded and renew them only
   after verified progress.
5. Measure the bump's actual selection-to-promotion latency and cold-cache CI,
   plus the age of the oldest eligible unrepresented approval. Exercise
   restart, lost webhook, quota refusal, failure bisection, and withdrawal
   paths in production canaries with recorded outcomes.

At increasing arrival rates, average throughput is insufficient: queue age and
tail latency determine whether the pin can land before its deadline. Keep
capacity headroom and bound unrelated work admitted ahead of the bump. Scaling
should stop at the first violated queue-age, quota, or runner-capacity bound,
then address that bottleneck. The deployed recovery mechanism is small and
reuses Bors, but the three-day target remains unproven until repeated fresh-pin
updates complete within their remaining age budget.

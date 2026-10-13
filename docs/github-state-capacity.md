# GitHub state capacity evidence

The new read service removes the multiplication of repository surveys by worker
count. Live query measurements support the modeled read load at a $1,500/hour
run rate, under the explicit workload assumptions below. They do not establish
an unconditional fleet throughput guarantee. The initial and client-stage
observation windows are one hour each. As of **2026-10-12 11:27 UTC**, all nine
existing science workers have applied the service read policy. Comparison
sampling continues; the staged rollout is not a production capacity load test.

## Current fleet evidence

Sample: **2026-10-12 05:56:16 UTC**, preceding **4 hours**, 9 enabled science workers.
Settled model expenditure including reviews was $43.23/hour,
or $4.80/worker/hour. Instrumented traffic averaged
526.2 REST core attempts/worker/hour, excluding quota probes
and uncharged 304 responses. GraphQL averaged
84.6 HTTP attempts/worker/hour and
69.7 explicitly measured points/worker/hour.

The ledger includes failed attempts and retries, and some native CLI requests
lack explicit GraphQL point charges. Direct HTTP and controller probes are
outside this host instrumentation. Actual successful request rates and model
utilization are depressed by quota waits. Multiplying these observed rates to
$1,500/hour gives roughly 164,314 REST attempts/hour
and at least 21,754 known GraphQL points/hour.
This illustrates the current architecture's scaling problem; it is not a
forecast of unconstrained useful work. The implied 312 workers
assume the same utilization and are not a recommended fleet size.

The machine-readable evidence, including endpoint and per-worker breakdowns,
is retained at `~/.local/state/tauceti/science-ramp/github-state-capacity.json`.
Regenerate it with `python3 scripts/github-state-capacity.py --hours 4`.
Regular four-hour reports now distinguish worker-account traffic from the
production App's repository-wide read usage and show its sample time. App
usage amortized over worker count is not causal per-worker attribution.

## Measured query costs

The dedicated test installation measured the production query builders on
October 12. Two samples each of the larger profiles found:

| Query | Items | GraphQL points | Elapsed time |
| --- | ---: | ---: | ---: |
| Issue/PR conversation histories | 100 | 1 | 2.30–2.50 s |
| Commit statuses | 100 | 1 | 1.27–1.45 s |
| Review-comment reactions | 100 | 1 | 0.79–0.82 s |
| Review threads, ten initial threads per PR | 10 PRs | 1 | Separate initial profile |
| Review threads, twenty initial threads per PR | 10 PRs | 2 | Separate initial profile |

The hundred-item samples had no outer or nested overflows. That result is
specific to the sampled histories. Dense histories continue through pagination;
partial or errored aliases never become complete empty histories. One failed
alias leaves the other successful batch items usable. Every request still
records its own `rateLimit.cost`; header differences are not point accounting.
The service and model share batch sizes from `github_catalog.json`.

## Repository workload model

Both installation tokens currently report separate **5,600/hour REST core and
GraphQL pools**. The following forecasts conservatively compare against 5,000.
The service uses REST for efficient or REST-only detail, files, checks, workflow
jobs, and references; GraphQL batches independent histories, statuses, reactions,
and review threads. For equivalent reads, observed cost/headroom drives route
selection, with measured latency breaking close choices. Mid-page exhaustion
restarts staging in the compatible pool without mixing cursor formats.

| Open PRs | Recently demanded PRs | Changed PRs/hour | GraphQL points/hour | REST requests/hour |
| ---: | ---: | ---: | ---: | ---: |
| 1,000 | 200 | 100 | 642 | 998 |
| 2,000 | 400 | 100 | 1,162 | 1,332 |
| 5,000 | 400 | 100 | 1,577 | 2,332 |

Revision 2 scenarios assume one important active CI run and one important
completion per hour, with no extra requested workflow lists.
These scenarios assume five reaction-bearing review comments per hot PR and
twenty per quiet PR; hot history/status/reaction audit intervals are five
minutes. Review-thread batches start with ten threads and twenty comments.
Inventories reconcile hourly, deltas run each minute, checks and reviews audit
on a six-hour interval, and quiet reaction audits amortize daily. Webhooks
materialize changes directly instead of making each event start a full survey.
Completed workflow jobs and quiet closed PR histories stop periodic polling.
Client demand reactivates them. These server policies do not impose client age
thresholds or authoritative validation before model spending.

The connection to dollars is conditional: a $1,500/hour fleet creating about
100–200 changed PRs/hour spends about $15–$7.50 per changed-PR event, inclusive
of authoring and reviews. A changed-PR event is not a completed PR. Once the
canaries run, replace that assumption with observed event counts and settled
costs from the same time window. Worker count, turnover, active demand, history
density, and revisit rates are separate model inputs.

## Reaction-density stress

For 5,000 open PRs, 800 recently demanded PRs, and 200 changes/hour:

| Hot reaction-bearing comments/PR | GraphQL points/hour | REST requests/hour |
| ---: | ---: | ---: |
| 5 | 2,343 | 2,934 |
| 10 | 2,823 | 2,934 |
| 20 | 3,783 | 2,934 |

At the densest modeled case, each read pool still fits its observed installation
limit. Larger reaction batches are what made this possible: the earlier
25-item design projected 10,482 GraphQL points/hour in that case. Increasing
batch size to 100 materially reduces the one-point minimum overhead. Additional
pages, much denser threads, frequently revisited closed PRs, or an extended
recovery burst could consume that headroom. These are scenarios, not measured
production load tests.

## Exactness and reduced pressure

Clients always ask for the newest available mirror result and accept its age.
Transport failures, missing coverage, invalid protocol/auth, and unavailable
storage use the same direct facade as fallback. A client does not repeatedly
probe GitHub to validate otherwise operable service data.

The chosen balance is immediate webhook updates plus bounded reconciliation
within the App quota. Reaction removals and missed webhook changes can remain
visible until their next background audit. Quiet closed histories retain a
complete baseline and webhook updates rather than being polled indefinitely.
That trades detection delay on rarely used history for quota that can serve
fast-moving work. Client demand schedules a new repair, and a webhook arriving
during a staged read keeps its newer repair pending.

For stronger detection, shorten the affected server audit interval; the model
scales that component accordingly. For less pressure, lengthen quiet audits or
reduce the hot set. Neither choice adds a per-query spending gate. More exact
reaction tracking is particularly expensive. The
[GitHub webhook event catalog](https://docs.github.com/en/webhooks/webhook-events-and-payloads)
lists no reaction-change event, so the design treats reaction audits as
necessary alongside comment and CI webhooks.

Bootstrap/recovery bursts, nested pagination, unsupported native CLI reads,
GitHub content/secondary write limits, Cloudflare compute/storage costs, and
long-history memory behavior need separate validation. Writes remain on existing
worker credentials with graceful waits after actual refusals and new-category
alerts. This implementation adds no preemptive write pacing and does not
implement staging repositories or change PR sizing.

The operating and rollout instructions are in
[the service runbook](../services/github-state/README.md).


## Reconciliation revision 2

The previous model included two compulsory workflow-job baselines per changed
PR. Revision 2 removes that obligation for undemanded CI. The replacement is
`60 × important active runs + important completions/hour + requested complete
workflow lists/hour`; all three are explicit model inputs. At 100 changed
PRs/hour, one active important run, one completion/hour, and zero requested
workflow lists, the modeled CI component falls from 200 to 61 REST requests
per hour. That 139-request saving is conditional, not a production measurement.
Additional pages, run events requiring immediate repair, and retries add cost.

The capacity script emits before/after scenarios alongside timestamped shared
App accounting, query shapes, lane depth, and repair completions from the
comparison sampler. Use `--important-active-runs`, `--important-completions`,
and `--requested-workflow-runs` to vary CI demand. Worker fallback traffic and
exclusive App traffic remain separate. Higher concurrency reduces queueing;
it does not increase either primary quota. A small optional backlog can be
healthy while requested coverage stays available.

The new acceptance windows are one hour in test and one hour in production.
Replay tests establish scheduling, consistency, and quota accounting; they do
not prove real Cloudflare/GitHub throughput at $1,500/hour. Replace the model's
turnover, density, pagination, and fallback assumptions with observations from
these windows before making that claim.

An October 13 test probe issued 160 cached scalar PR reads from one host. All
succeeded. With four concurrent requests, throughput was 27.3 reads/second and
p95 latency was 198 ms; with sixteen, throughput was 73.7 reads/second and p95
latency was 165 ms. Responses averaged 6.2 kB. These reads exercised the live
Cloudflare service and its stored PR state. They did not measure large
inventories, long histories, a sustained fleet workload, or GitHub writes.

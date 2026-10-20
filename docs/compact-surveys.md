# Compact roadmap surveys

Automatic work rounds consider existing PR maintenance and reviews across all
non-skipped roadmaps before choosing an area for new authoring. Within each
maintenance stage, older PR numbers come first. A Git-based branch-claim hint
skips known peer work before fresh PR reads, without REST or GraphQL requests.
Atomic acquisition remains authoritative; losing a race refunds the unlaunched
retry. Claims and fresh launch-time checks skip unavailable work and continue
through the remaining candidates.
The shared GitHub state service serves a compact projection of open PRs and
their scoreboard, review claims, newest contest reply, and trusted rebase
handoff heads. Workers keep the scheduling cascade, budgets, counters, reviewer
affinity, and task selection. No model work or GitHub writes happen on the server.

## Request and coverage

`survey` is a normal authenticated `/v1/query` operation. Its arguments are
`roadmap`, comma-separated `skip`, and optional comma-separated PR `numbers`.
An explicit worker `--pr` overrides the ambient roadmap. An explicit concrete
`--roadmap-only` retains its scope, and task restrictions retain their meaning.
Unrestricted rounds select an authoring area only after the existing-work cascade
finds no available candidate. Status and dashboard calls retain their existing
full inventory view.
Automatic authoring limits still count all non-skipped roadmap PRs by that
identity. The service supplies small inventory-wide author counts alongside the
scoped rows; other roadmaps' histories are not fetched for this count.

Known roadmap labels take precedence over description target markers. A specific
roadmap overrides an overlapping skip. Unassigned maintenance PRs and unknown
roadmap assignments remain in a shared pool until their labels identify an area;
missing labels cannot strand repairs. Work stages disabled by `--only`/`--skip`
do not inspect their histories.

Each row carries `tauceti-survey-facts/v1`, compact PR metadata, and independent
`issue_complete` and `review_complete` flags. The envelope's complete coverage
means the open PR inventory is complete. Per-row history flags determine which
facts can replace full histories. Global automatic discovery requests `stages=""`
so the broader scan creates no per-PR history hydration demand. Only completely
cached histories enter review/fix classification; contest reaction claims are
checked for the selected candidate at dispatch. Missing necessary coverage is
reported and prevents new authoring, while known actionable work can proceed.
The configured shared service becoming unavailable pauses global discovery;
it never triggers a bulk direct-history fallback. The round exits as a typed
dependency wait (`EX_GITHUB_WAIT`), not as no-work: it charges no PR repair
allowance and no no-progress backoff, and the runtime status records
`wait_dependency`, `wait_reason` (for example `http_503`, `timeout`,
`connection`, `invalid_response`, `incompatible_protocol`,
`coverage_unavailable`, `mixed_revision`) and a bounded, credential-free
`wait_detail`. The worker rechecks at the shared circuit's jittered retry time
and resumes on its own once a single half-open probe succeeds. Client
authentication or configuration failure is instead reported as a failed round
that names the operator action. A retained complete response keeps its
revision, coverage and observed/verified timestamps bound to its query and
client identity; it informs the wait report but never stands in for current
coverage or an empty inventory. Explicitly scoped rounds keep
the existing fallback, and installations without a shared service continue to
use the direct facade and its existing transport budgets.

A missing scoreboard in a complete issue
history means absence; missing issue coverage uses the existing comment reader.
Partial review coverage similarly uses the existing contest reader. Claim expiry
is checked against the worker clock and the exact head, never against projection
time. Existing targeted checks and post-write cache invalidation discard the
survey seed for that PR.

Clients always request the latest available projection. Age does not trigger an
extra upstream read. For explicitly scoped rounds, outage, authentication
failure, unsupported operation, or incompatible/malformed contract uses the
existing direct survey and comment facade. It applies the same roadmap filtering before reading histories, and
retains the existing REST/GraphQL accounting and quota recovery paths. An empty
successful survey is distinct from an unavailable service.

## Materialization and updates

SQLite `survey_inventory` stores small PR headers and target focuses.
`survey_fragments` stores only useful comment facts, without prose or model token
data. `survey_state` reduces those fragments over current collection membership.
Accepted webhooks and reconciliation results update these tables in the same
object as the original resources. Edits, deletion tombstones, and membership
removals dirty the affected PR; coalesced rebuilds run in maintenance or before a
query reads that summary. Bursts do not rebuild once per comment. An older
response cannot overwrite a newer event. Status/check updates are joined only
for selected PR heads.

Existing stored histories migrate locally in bounded batches, with revision
checks across asynchronous resource reads. No additional GitHub requests are
needed to materialize complete stored histories. While migration is incomplete,
history coverage remains false and workers retain fallback. Migration runs only
in maintenance; failures retry per PR without blocking other materialization or
cleanup. Normal background
reconciliation fills genuine coverage gaps under the installation's existing
quota scheduler; serving a survey never synchronously validates against GitHub.
Complete cached histories are not marked as new demand by a survey: webhooks
and normal background verification maintain them. Only missing, relevant
histories for enabled stages get requested-gap priority. Red/pending builds and
disabled stages do not create review-history demand.

Warm queries scan compact open PR headers, then read only selected summaries and
statuses. Full comment histories are not scanned or returned. Stable pagination
uses the normal 500-row/1.49 MB bounds. Healthy scoped inventories typically fit
in one request. `/v1/health` reports the contract and open/materialized/hydrated
counts. Host transport telemetry adds `survey_result` with source, roadmap, PR
count, coverage gaps, and total elapsed time; gateway telemetry retains request
latency and revision.

## Keeping the implementations aligned

The Python fallback and TypeScript projection import
`tauceti_worker/survey_contract.json`, which defines marker syntax and retained
metadata fields. Pure reductions live in `survey_facts.py` and the service's
`src/survey.ts`. The service test generates 240 deterministic changing histories
and scopes from Python and compares TypeScript output, including malformed
markers, head claims, and identifiers above JavaScript's safe integer range.
Worker tests compare actual task selectors through compact and direct paths
and verify outages, partial coverage, claim expiry, and scoped history reads.
A 5,000-PR service fixture checks that roadmap filtering precedes status/history
retrieval and returns a small single-page response. Repository tests verify
edits and deletions against the materialized view.

Changes to reduction semantics must update both pure implementations and the
differential fixtures. Incompatible wire or semantic changes must change the
contract ID so older clients fall back instead of misinterpreting facts. Deploy
the service first, validate test data against full stored histories, then update
workers at idle round boundaries. Worker count and accrued grants are untouched.

Read-only measurements compare compact facts against full histories already
stored in the mirror, without reference reads through GitHub:

```sh
python3 services/github-state/scripts/measure_surveys.py \
  --environment test --roadmap PDE --roadmap OptimalTransport \
  --compare 12 --output /tmp/survey-measurement.json
```

The output includes response size, latency, request count, coverage gaps, and
sampled differences. Comparisons prioritize distinct roadmap assignments; raw
comment contents and credentials are excluded.

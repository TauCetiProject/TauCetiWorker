# Converge GitHub and Forgejo worker execution

Status: implementation in progress. Updated 2026-11-11.

Fix the failures exposed by the [live Forgejo worker smoke PR](https://git.taucetiproject.org/TauCetiProject/TauCeti/pulls/3)
by making GitHub and Forgejo use the same worker workflow. Minimize parallel
implementations, localize unavoidable source differences, and abstract those
differences behind the existing source interfaces.

## Evidence and scope

The smoke used Worker `ba9b608cf`. PR creation, discovery, blocking review
publication, branch claims, safe pushing, inline replies, publication replay,
stale-head refusal, and PR closure worked. Automatic repair failed before
inference; the defect was repaired directly and checked with stock Lean. The
subsequent normal review timed out during input preparation.

| Observed failure | Connection to the new Forgejo path |
| --- | --- |
| Lake cannot materialize a partial Mathlib clone | Source activation exports `GIT_NO_LAZY_FETCH=1` into the whole round, including dependency preparation. |
| Archive sync fails with `transport 'file' not allowed` | The source environment's Git protocol restrictions reach `uvx` and prevent its local cached package clone. The outbox survives. |
| Review preparation transfers full history twice, then can time out | The separate Forgejo preparer fetches the base and PR head into a fresh repository in two operations. The first review stored about 520 MB of packs; the next review failed with a preparation timeout before inference. |
| Source-checkout review raises `PackageNotFoundError` | The Forgejo runner assumes an installed review distribution and installed Worker metadata. The existing GitHub host path loads the review engine on demand. |
| Familiar agent commands return `Unsupported` | The compatibility wrapper lacks `pr diff`, `pr diff --name-only`, `pr checks`, and `pr view --comments`. |

These are Worker/Tools integration defects; the smoke did not establish defects
in the Forgejo server or corresponding failures in ordinary GitHub rounds.
The shell installation gap was separately resolved by updating its managed
runtime to `96d7d1300` and verifying Forgejo-only discovery. General cache
optimization remains in the [cache preparation plan](cache-preparation-efficiency.md).

## Required architecture

Use one implementation of scheduling policy, review/repair coordination, tool
resolution, input preparation, engine invocation, result classification,
publication reconciliation, and archive delivery. Source adapters provide the
operations and normalized data that implementation consumes.

The present branch from `work_units.do_review` to `source_execution.review`,
plus the separate preparation in `source_review.py` and the review CLI, is a
specific convergence target. Extract or generalize existing code and make both
callers delegate to it. Remove superseded orchestration and argument builders
as each common component lands. A compatibility entrypoint may remain as a
thin delegate; it must not retain a second implementation.

Keep responsibilities close to their existing owners: qualified identities and
forge transports in TauCetiTools, review-engine preparation and execution in
TauCetiReview, and worker scheduling/recovery in TauCetiWorker. Use the existing
`Forge`, repository identity, source actor, candidate, and prepared-input
representations wherever possible. Introduce only the small interfaces needed
to share actual behavior.

| Boundary | Common behavior | Difference confined to the adapter |
| --- | --- | --- |
| Source reads | Validate identity, normalize PR/discussion data, expose freshness and unavailability | GitHub API/shared-state reads versus Forgejo REST reads; native response shapes |
| Git inputs | Fetch, negotiate/reuse objects, verify selected commits, compute merge base and diff | Qualified clone URL, PR ref discovery, origin-bound credential selection |
| Claims and publication | Acquire/renew/release, reconcile retries, reject stale heads, retain paid output | Claim storage primitive, native comment/thread operations and supported capabilities |
| Review execution | Resolve engine, prepare inputs, invoke once, classify result, archive | Source metadata supplied as data; host/sandbox execution remains a separate capability |
| CI observations | Represent execution evidence and unavailable/pending states | The configured execution backend and its evidence, independently of the PR's source forge |

Prefer capabilities and normalized operations over scattered `forge.kind`
branches. Keep unavoidable forge selection in adapter construction and transport
implementations. Preserve native identity, authorization, and thread semantics
when mapping responses; unsupported capabilities must remain explicit. CI
evidence must not become successful merely because a native status is absent.

Treat convergence as a requirement for each fix, not a later cleanup:

- A forge flag selects a source adapter, not a separate review or repair runner.
- Put workflow decisions in common code. Adapters expose operations, normalized
  records, and capabilities; they do not own scheduling or retry policy.
- Reuse a common transport helper when only an endpoint or response shape
  differs. Add an interface only when an actual backend difference requires it.
- Remove the replaced path in the same change. Do not retain a GitHub path and
  a Forgejo copy as alternative implementations of the same lifecycle stage.
- Test the shared behavior once with both adapters. Keep backend-specific tests
  focused on protocol, identity, authorization, and native thread semantics.

Each implementation PR should identify the shared entrypoint, the old paths it
removes, and any remaining forge-specific branches with their concrete reason.
Reject new parallel orchestration even if both copies currently pass tests.

## Implementation order

### 1. Share runtime setup, tool resolution, and archive delivery

Inventory the current GitHub and Forgejo call chains and assign each lifecycle
stage to its common implementation before making the fixes below. Keep this a
bounded extraction around the observed failures; unrelated worker subsystems
can continue using their existing interfaces.

Make environment construction specific to the operation. Origin-bound source
fetches, claims, and publication keep their appropriate strict Git settings.
Dependency setup receives an environment that supports stock Lake partial
clones. Tool loading receives the environment it needs for trusted local package
caches. Carry provider settings and commit attribution explicitly, and prevent
source credentials and incidental Git restrictions from becoming round-wide
process defaults. Apply the same environment boundaries for both forges.

Use one on-demand review-engine resolver for GitHub and Forgejo. Preserve the
existing configuration/override precedence and honor an already configured
revision. Both the source shim and an installed wheel must use it without
assuming that the Worker distribution has installed metadata.

**Do not add another version or commit pin to `./tauceti`.** Repair the new
path's installation assumption through the shared resolver. Any necessary
dependency revision updates belong in the existing authoritative configuration
and dependency-update flow, without inventing another pin location.

Resolve the engine once for a round and use that same resolution for review and
archive delivery. Prefer invoking its existing archive entrypoint directly so
sync does not independently install another engine through `uvx`. Keep this a
common archive path for both sources. Preserve the outbox on failure and allow
later delivery to retry without repeating inference or review publication.

Validation: use a fresh partial dependency clone, a clean PEP 723 script
environment, and a clean wheel installation. Exercise archive failure followed
by retry. Verify operation-appropriate credentials and Git settings, successful
dependency checkout, and preservation of unpublished records.

### 2. Use one review preparation and execution pipeline

Give the shared review runner normalized source metadata and the existing
prepared-input contract. Share source staging, description/history/reply
preparation, roadmap and Mathlib context, engine argument construction, result
handling, and cleanup. Both adapters must enter this same pipeline; moving the
Forgejo copy to a differently named module alone does not achieve convergence.

Generalize the existing Git preparation/cache implementation to accept a
qualified source and explicit commit/ref inputs. Fetch the required base and
head together into named refs, and reuse available repository objects across
reviews. Respect existing repository and credential isolation when choosing a
cache. Verify the fetched head against the selected head before inference and
recheck it at publication. Preserve exact merge-base and diff semantics.

Use shared preparation deadlines and failure classification. Emit the current
step and elapsed time, including fetch, dependency setup, and resource waits.
Preparation failure must not spend a PR's inference-error allowance. Preserve
rendered paid output if publication needs retry, and clean generated inputs and
child processes according to the same lifecycle for both forges.

Move common publication retry/recovery policy into that lifecycle. Native API
operations and native thread mapping stay in adapters; there must be one policy
for duplicate suppression, stale-head refusal, and retained output recovery.
Preserve existing GitHub stores, qualified Forgejo stores, claims, counters,
and pending paid work throughout the refactor.

Validation: run the same review scenarios against GitHub and Forgejo adapters:
initial review, a changed head, an unchanged-head retry, a contest reply, failed
preparation, interrupted publication, and recovery. A subsequent review must
reuse existing objects rather than transfer full history again. Retain the
existing claim-collision and stale-push/publication checks. Test behavior and
transfer/recovery outcomes rather than exact internal helper call counts.

### 3. Complete the observed agent command contract

Implement the missing commands in the TauCetiTools compatibility layer using
the same source-read abstractions as worker preparation:

- `gh pr diff`, including `--name-only`;
- `gh pr view --comments`;
- `gh pr checks`, with meaningful unavailable/pending behavior when execution
  evidence has not been supplied.

Keep parsing, rendering, pagination, and supported exit behavior common where
the semantics match. Put native endpoint and response mapping in adapters.
Avoid teaching repair prompts or the scheduler separate command recipes for
GitHub and Forgejo. Document any remaining unsupported options explicitly.

Run one command-contract suite against both backends, supplemented by small
native-API tests for unavoidable differences. Verify qualified routing when PR
numbers overlap and that source observations cannot invent CI authority. Land
the Tools support before updating Worker's existing Tools dependency reference.

## Acceptance and delivery

Use the existing GitHub and native HTTPS Forgejo fixtures throughout the
extractions. Share the workflow scenarios and assertions, with adapter-specific
setup and native-operation assertions where necessary. Keep coverage for
GitHub's current review, contest, quota, claim, archive, and recovery behavior
as it moves onto the common implementation.

After the focused checks pass, exercise a fresh real PR through creation,
blocking review, automatic repair, review of the repaired head, archive sync,
and closure. Verify the original finding is re-evaluated on the new head. Use
standard worker commands and stock Lean/Lake. Confirm stale writes are refused,
replays do not duplicate publications, and interrupted work remains recoverable.
Record whether any outstanding rubric is an actual content judgment or a
workflow failure; do not equate successful review execution with an all-green PR.

Deliver the shared extractions with their fixes so there is no permanent
GitHub implementation plus Forgejo workaround. Update the normal managed
runtime after merge and verify the deployed entrypoint. Any live worker rollout
keeps admissions enabled and checks running revisions, useful completions, and
interrupted work. This plan does not change source defaults, merge-queue
authority, or enable an unimplemented sandbox capability.

Completion means the observed failures are fixed through common code, the
normal Forgejo repair/re-review cycle completes, GitHub behavior remains
covered, and the remaining forge differences are identifiable at adapter
boundaries.

## Implementation record (2026-11-11)

- Tools [PR #235](https://github.com/TauCetiProject/TauCetiTools/pull/235)
  merged at `67a371b8dc3568e811257483d16fcb728c0261cc`, with all hosted
  package, stock Lean, workflow-identity and state-service checks passing.
- Review [PR #229](https://github.com/TauCetiProject/TauCetiReview/pull/229)
  contains the shared lifecycle and passes all 36 standalone modules locally and
  in CI. Merge requires a code-owner approval. Worker's existing dependency
  reference temporarily names its reviewed head; replace it with the merge
  commit before landing Worker.
- Shared engine scenarios cover initial/unchanged/changed-head review for both
  adapters. Publication covers lost responses, live locks, original identity,
  legacy native receipts, conversation edits and archive retention. A disposable
  stock HTTPS Forgejo passed claims, shared preparation/publication, recovery,
  contests, replies and stale-head refusal.
- A clean Python 3.11 environment containing Tools alone runs the shared engine
  scenarios. An installed Worker wheel resolves and launches the engine without
  a Review distribution. The source launcher retains its existing dependency
  surface, updating only its existing Tools reference.
- Worker full-suite validation, fresh real PR automatic repair/re-review,
  archive delivery, closure and managed-runtime update remain outstanding.

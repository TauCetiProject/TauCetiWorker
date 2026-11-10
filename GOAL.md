# Move TauCeti development to Forgejo

Make `https://git.taucetiproject.org/` the home of development for the main
TauCeti repository. Forgejo owns contributions, reviews, and merges; its `main`
branch automatically mirrors to GitHub. Keep GitHub Actions for CI and keep
familiar agent-facing `gh` commands through the existing compatibility layer.

Prioritize the earliest practical migration. **Stage 1 delivers a usable,
human-supervised development path and a rehearsed migration package. Stage 2
completes autonomous operation and the remaining integrations.**

**Finishing Stage 2 is not a prerequisite for migrating the main repository.**

| Stage | Deliverable | Completion gate |
| --- | --- | --- |
| **1: Ready to migrate** | Forgejo contributions, supervised review and merge, real GitHub CI, automatic `main` mirroring, and working builds, caches, and docs. | End-to-end rehearsal, preserved active work, and an executable cutover/rollback package ready for an explicit cutover decision. |
| **2: Complete the transition** | Autonomous Worker/Review/Progress and queue operation, remaining portability and fleet work, and shared-state retirement preparation. | Full lifecycle, recovery, capacity, installed integration, and operational handover evidence. |

Work on Stage 1 first. Bring a Stage 2 item forward only when a demonstrated
Stage 1 blocker requires it; record why and the resulting change to the critical
path. Do not turn the urgent migration into a prerequisite to finish every
integration. Live migration and eventual service shutdown each require separate
authorization.

These are requirements and completion gates, not a claim that every item remains
unimplemented. Reuse completed work and record the evidence; do not rebuild it.

Tracker: [TauCetiWorker #602](https://github.com/TauCetiProject/TauCetiWorker/issues/602).
The [original work packages](https://github.com/TauCetiProject/TauCetiTools/blob/8f30faab8f3545ce688c199c34efa780c0603111/docs/forgejo-preparation.md)
and [execution record](docs/forgejo-preparation.md) remain references. This document
replaces their all-or-nothing delivery order with the two stages below; it does
not discard the unfinished requirements.

## Operating rules

- Keep familiar agent-facing `gh` commands through the existing compatibility
  layer. Unsupported native operations must fail explicitly, never act on the
  GitHub mirror by accident.
- Use qualified forge/repository identities, origin-scoped credentials, trusted
  configuration, and immutable tool revisions. Support Forgejo source alongside
  GitHub-hosted tools, dependencies, and CI.
- Preserve approval authority, candidate sandboxing, scope and dependency/bump
  checks, exact-candidate CI evidence, and controlled branch promotion. A human
  operating the workflow does not waive these protections.
- Reuse existing Git refs, queue/review ledgers, publication outboxes, and bounded
  caches. **No repository storage redesign, path identity registry, or new storage
  backend is part of this goal.**
- Preserve existing spending history and prepared work. Migration and recovery
  must not reset budgets or repeat paid authoring/reviewing.
- Retire the shared-state service after its consumers migrate; do not generalize
  or redesign it. Shutdown requires separate explicit authorization. Keep it
  operational for consumers that still need it.
- Prefer existing implemented components. Make new changes only where they close
  a requirement or a demonstrated failure in the current stage.
- Production remains on GitHub until a separately authorized cutover. This goal
  document does not authorize live imports, mirror activation, deployment of a
  Forgejo fork, changes to production merge ownership, or service shutdown.
- Keep normal worker and CI admissions enabled during preparation. Do not drain
  the fleet as a routine precaution. The eventual cutover must explicitly account
  for in-flight work and transfer authority for the affected project; it must not
  leave GitHub and Forgejo acting as independent merge authorities.

## Stage 1 — Prepare and rehearse the earliest operational migration

### Deliverable and critical path

Deliver a development path an operator can actually use, plus the evidence and
commands needed to authorize cutover. This stage prepares and rehearses migration;
it does not itself switch production authority.

Use these priorities, running independent preparation in parallel. Confirm
destination access and recovery ownership, and collect the active-work inventory,
while connecting the existing candidate path to real CI:

1. Confirm destination access and recovery ownership.
2. Connect the existing source-candidate path to a real GitHub Lean build.
3. Complete the human-operated review, dispatch, verification, and merge commands.
4. Prove automatic `main` mirroring and essential post-merge outputs.
5. Rehearse interruption recovery and rollback; deliver the cutover package.

Track unresolved access and operational dependencies explicitly. More Stage 2
implementation is not a substitute for resolving an essential migration blocker.

### Intended operating mode

Start development on Forgejo with humans supervising review, CI dispatch, and
merge operations where automation is not yet ready. Reuse working automation,
but do not require the complete autonomous fleet on day one. Record the temporary
manual responsibilities and their owners in the cutover runbook.

Stock Forgejo is sufficient if this path passes its tests. Do not make the private
server API additions or autonomous queue a Stage 1 dependency unless a concrete
requirement of the chosen operating mode needs them. If uninterrupted autonomous
operation becomes a day-one requirement, explicitly promote its prerequisites
from Stage 2 and revise the critical path; do not silently bypass their gates.

Supporting repositories and immutable tool/dependency pins may remain on GitHub.
Comprehensive GraphQL compatibility and a full-fleet rollout are not Stage 1 work.

### 1. Establish the destination and preserve development work

- Confirm the destination organization/repository paths, operator permissions,
  contributor access, and the people responsible for Forgejo operation and recovery.
- Verify usable backups and recovery access for the destination's Git data and
  relevant database/configuration. Test recovery in an isolated destination.
- Import or copy the Git history needed for development, including required
  branches, tags, and objects referenced by current dependency pins. Preserve
  commit identities and verify them against the source.
- Inventory active PRs, contributor branches/forks, queue candidates, claims, and
  unfinished paid work. Specify what transfers, what finishes on GitHub, and what
  needs a fresh review or build. Retain the old references until work is settled.
- Test clone, fetch, contributor push, fork contribution, PR review, branch
  protection, and authorized merge access on the actual supported server version.
- Keep historical GitHub discussion readable. Full historical issue/PR import is
  not a cutover prerequisite. Record explicit correspondence for transferred
  objects; never reinterpret an old GitHub number or approval as native authority.

### 2. Connect real GitHub CI to Forgejo candidates

This is the first engineering priority after destination access. Use the merged
source-candidate contracts and trusted build implementation rather than creating
another CI system.

- Configure the public GitHub execution repository independently of the Forgejo
  source repository. Install a protected caller with tested immutable tooling pins
  and narrowly scoped credentials. Verify access to the intended runner pools.
- Provide a practical operator command to prepare and dispatch a candidate and
  retrieve its result. Automatic webhook/queue dispatch may follow in Stage 2.
- Retain immutable source head, base, and candidate objects on Forgejo. GitHub
  runners fetch them directly; pre-merge candidate branches need not be mirrored
  to GitHub.
- Validate the configured source identity, candidate commit/tree, trusted harness,
  request, run, and attempt. Preserve existing sandboxing, lint/scope checks,
  dependency/bump policy, diagnostics, and required build/cache behavior.
- Demonstrate a real production-shaped Lean build on GitHub Actions. A dispatch
  token probe, synthetic success, local build, or fake CI response is insufficient.
- Make failures and unavailable results visible. A changed PR head, superseded
  request, wrong repository, stale result, or failed build must not authorize merge.

### 3. Provide controlled review and merge

- Define who reviews, dispatches CI, verifies results, and authorizes a merge in the
  temporary human-operated mode. Make these commands usable without completing
  autonomous Review publication.
- Bind review acceptance to the current source revision and applicable policy.
  Imported approvals and editable PR prose do not grant authority.
- Promote the exact tested candidate using the expected current base. If that
  base or the accepted PR head changes, re-evaluate and rebuild as required;
  do not merge an untested reconstruction through the web UI.
- Verify protection and credential boundaries. Candidate execution must not receive
  source merge credentials, and ordinary contributors must not bypass the merge path.
- Preserve branch/work artifacts on ambiguous outcomes and reconcile before retry.

### 4. Prove the main mirror and essential post-merge outputs

- Prepare a Forgejo-to-GitHub push mirror with an explicit `main` branch filter.
  Retain GitHub as the CI execution home and historical reference.
- Verify that normal mirror pushes trigger the intended GitHub main workflows and
  that both repositories report the same `main` commit.
- Run the actual post-merge build, Lake cache publication, and documentation
  publication. Verify their revision identities, artifacts, and usable outputs.
- Update the provenance readers needed by these paths to understand new qualified
  `Source-PR` citations while retaining historical GitHub conventions. Native PR
  numbers must never be looked up on GitHub by coincidence.
- Classify the remaining GitHub automations: retain execution/artifact jobs, port
  essential development behavior, and prevent obsolete source-writing jobs from
  acting on the mirror. Document temporary manual replacements.
- Supporting repositories, immutable tool pins, and dependency locations may remain
  on GitHub. Move only what the Stage 1 path actually requires.

### 5. Rehearse and deliver the cutover package

Use isolated destinations and dedicated credentials. Run at least:

1. A contributor creates a Forgejo PR using the supported Git/`gh` path.
2. A human reviews it and dispatches the real GitHub candidate build.
3. A failing candidate is refused; a corrected revision passes.
4. A changed head or base cannot reuse stale acceptance or CI success.
5. An operator lands the tested candidate on Forgejo.
6. `main` mirrors to GitHub with identical commit IDs.
7. Main builds, caches, and documentation publish successfully.
8. An interrupted dispatch or publication is recovered without duplicate merge
   or repeated paid work; a mirror failure can be detected and repaired.
9. The rollback procedure is exercised in isolation, including a contribution
   landed after the simulated cutover. Preserve that work when returning authority.

Deliver a concise cutover/rollback runbook, the exact commands and pins, linked
test evidence, the active-work transfer inventory, and the named temporary manual
responsibilities. Include the authority switch, contributor remote changes,
GitHub automation changes, backups, verification, and how to settle interrupted work.

### Stage 1 completion and cutover boundary

Stage 1 is complete when the operator can review evidence for all six gates:

| Gate | Required evidence |
| --- | --- |
| **Destination** | Intended destination access, protections, backup/recovery, and operational owners confirmed. |
| **Work preservation** | Required Git objects and active work accounted for, including claims, spending records, prepared outputs, and transfer decisions. |
| **Contribution path** | A real failed candidate is refused, its corrected revision passes GitHub CI, and stale review/CI cannot authorize merge. |
| **Merge and outputs** | The exact tested candidate lands, only `main` mirrors, and actual builds/caches/docs produce verified outputs. |
| **Recovery** | Interrupted operations, mirror repair, and rollback exercised without losing contributions or repeating paid work. |
| **Handover** | Executable cutover/rollback runbooks, immutable pins, linked evidence, and temporary manual duties with named owners, with no essential-path blocker unresolved. |

It does **not** require autonomous Review/Progress, all support repositories moved,
full-fleet capacity validation, or shared-state shutdown. Present the migration
package for the cutover decision; unfinished Stage 2 items stay in Stage 2.

The live cutover remains a separate, explicitly authorized operation. After that
authorization, execute the rehearsed procedure and verify the running destinations,
useful completions, mirror, outputs, and any interrupted work. Readiness evidence
must not be reported as proof that the production migration already happened.

## Stage 2 — Complete autonomous operation and the remaining work

Restore the full development workflow on Forgejo without undoing the Stage 1
operating path. Work may proceed before or after the live cutover, but unfinished
Stage 2 features stay outside production admission until their own gates pass.

The sections below retain the remaining requirements from the original plan.
They are deliberately outside the Stage 1 migration gate: autonomous agents and
queue operation, necessary native API additions, broader compatibility and fleet
work, full-load validation, and shared-state retirement preparation.

### 1. Complete Worker, Review, Progress, and claims integration

- Finish worker discovery, selection, authoring, fork publication, repair, and
  recovery using qualified source identities and direct Forgejo reads.
- Complete Review preparation and trusted publication: revision-bound context,
  tokenless inference, authenticated publisher identities, scoreboards, findings,
  inline edits/replies, resolution/reopening, stale-head rejection, and retained
  publication recovery using the existing ledger.
- Finish Progress trusted runtime binding, native publication, qualified metadata
  and links, announcements, merge workflows, and Worker plan/facts/policy agreement.
  Preserve mathematical facts from Git/source artifacts and treat PR descriptions
  as unverified commentary.
- Integrate Git claims and test competing claims, expiry, interruption recovery,
  contributor permissions, and custom refs. Keep Actions disabled on claims repos.
- Verify spending limits and cost history across migration, restarts, and retries.
  Reconcile uncertain writes without repeating paid inference or relabelling old
  prepared output under new source/policy inputs.

### 2. Enable the autonomous queue and required native server capabilities

- Finish source/execution adapter wiring, publisher/approval integration, operator
  recovery commands, holds, restored-state handling, credential provisioning,
  narrower worker roles, and ingress concurrency/rate bounds.
- Preserve the existing controller, admission policy, repair machinery, Bors surface,
  and durable request/outbox records. Enforce current source facts, exact accepted
  heads, trusted publishers, and exact-candidate CI/merge guarantees.
- Test signed webhooks, duplicate/out-of-order delivery, missed events, cancellation,
  reruns, late results, missing runs, service/API outages, moderation, permission
  reduction, and restart recovery. Unavailable or incomplete evidence never grants
  approval or proves absence.
- Complete only the native Forgejo API changes required by these operations,
  including review/thread support and effective authority checks. Test the exact
  stock and patched versions and deliver a reproducible pinned patch release.
- Respect the pending human upstream-policy handoff: keep private native patches
  and findings private until that is resolved. Prepare focused upstream PRs through
  the permitted contribution route; do not publish or contact upstream automatically.

### 3. Complete portability, compatibility, fleet, archives, and installation

- Finish the integration inventory and operational command/query corpus. Exercise
  supported `gh` flags, JSON fields, filtering, pagination, mutations, authentication,
  and GitHub CI run/log access. Publish an accurate capability matrix with explicit
  failures for unsupported operations.
- Complete schema v1 compatibility and schema v2 mixed-hosting tests, including
  collisions in names, native user IDs, PR numbers, and execution run IDs across
  servers. Never share permissions or publication identities across origins.
- Classify and port all remaining workflows, labels, housekeeping, dependency
  updates, conflict checks, scheduled jobs, and provenance consumers. Backstops
  and timers must invoke the same canonical implementations.
- Complete Fleet source/execution credential separation, GitHub runner-provider
  configuration, activity summaries, and job classification. Keep the existing
  dependency-light remote deployment model and prepared-summary status commands.
- Version archive and reporting records to distinguish source repository/PR/head
  from execution repository/run/job/attempt and tested candidate/tree/harness.
  Preserve historical readers.
- Update installers, upgrades, templates, and download logic in their canonical
  Tools assets; regenerate downstream copies. Validate actual installed artifacts
  and consistent immutable dependency pins, not just source checkouts.
- Independently review and merge component PRs against upstream default branches,
  then test their actual integrated pins.

### 4. Prove the complete autonomous lifecycle and capacity

Demonstrate the full isolated sequence: roadmap selection and claim, authoring
through a contributor fork, published and revised review, repair of a deliberate
failure, queue admission, real GitHub CI, controlled merge, `main` mirror,
post-merge builds/caches/docs, and activity/archive/Progress updates.

Repeat across faults and component restarts with the shared-state endpoint blocked.
Use deterministic agent fixtures for the fault matrix and a small real-agent smoke
within existing task budgets; send notifications only to test destinations.

Seed production-representative histories and run at the actual current configured
fleet concurrency and a 2× burst. Establish a meaningful capacity baseline; zero
admissions at one observation is not a valid 0/0 load test. Record correctness,
selection latency, API request volume, Forgejo CPU/database load, CI observation
volume, backlog, and recovery. Resolve material bottlenecks, require no continuously
growing backlog at normal load, and demonstrate recovery after the burst.

Expand production automation only through separately authorized live updates.
Verify running revisions, useful completions, and interrupted or stranded work.

### 5. Prepare shared-state retirement and final handover

- Replace every Forgejo consumer's shared-state dependency with direct source reads
  and bounded local caching: surveys, selection, activity, queue reads, runner
  selection, CI collection, and reporting. Keep bounded direct GitHub reads for
  execution metadata.
- Instrument the complete rehearsal to prove there are no shared-state requests,
  including after restarts. Preserve distinct forbidden, incomplete, unavailable,
  stale, and ambiguous outcomes.
- Keep the existing service operational for remaining GitHub consumers. Retirement
  preparation must not become a redesign/generalization of that service.
- Deliver a tested retirement runbook covering consumer coverage, final archival
  exports, configuration changes, verification, recovery, and eventual shutdown.
  Actual shutdown requires a separate explicit authorization.
- Deliver reproducible rehearsal commands, linked results, merged revisions,
  installed/integrated pin evidence, the compatibility matrix, native patch release
  and permitted upstream handoff/PR records, and final operational runbooks.

### Stage 2 completion

Completion requires authoritative evidence for every remaining original requirement.
Component tests, green PR checks, and narrow fixtures cannot substitute for the
full lifecycle, installed integration, capacity, or operational recovery evidence.
Do not declare the whole goal complete merely because Stage 1 is ready or the
initial migration has succeeded.

## Separately tracked follow-ons

Full unmodified upstream `gh` support, including broad GitHub-compatible GraphQL
and REST semantics, remains a separate compatibility project. Maintain its tracking
issue and reusable command/query fixtures; completing that server compatibility is
not required by either stage. Native Forgejo runner provisioning also remains a
follow-on: GitHub Actions is the execution backend for this goal.

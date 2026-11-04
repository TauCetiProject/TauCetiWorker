# Tau Ceti redesign

This design develops a contribution system capable of integrating **100 contributions per hour, around 2,000 added Lean source lines per contribution**, across a core monorepo and downstream subject libraries. This is an operating target, not a measured capacity claim. The long-term ambition remains 100 times today's useful throughput; the historical baseline must establish how that ratio relates to the absolute target.

The governing policy is permissive reuse of agents' work. Reviews survive ordinary edits, rebases, dependency updates, and migration between systems. Agents decide when new information calls for additional review. The system prevents lost updates and duplicate execution without imposing repeated approval ceremonies.

The [canonical sample](evidence/README.md) contains 500 recent landed contributions with median 243.5 and mean 288.12 added Lean lines. The 2,000-line target represents a much larger workload per contribution. GitHub's ordinary merged-PR listing misses Bors squash landings and is unsuitable as the sole baseline.

## Documents

| Document | Purpose |
| --- | --- |
| [Current system](current-system.md) | Responsibilities, source of authority, worker lifecycle, review, CI, Bors, caches, recovery, and observed PR sizes |
| [Target system](target-system.md) | Contribution model, permissive review, independent library queues, hosting, operational model, capacity, and reuse choices |
| [Migration DAG](migration-plan.md) | Independently useful workstreams, prerequisites, deliverables, deployment boundaries, and completion criteria |
| [Shared protocol](specs/shared-protocol.md) | Common identities, snapshot format, API behavior, attempts, leases, evidence, events, and authority migration |
| [Lean build service](specs/lean-build-service.md) | Implementation handoff for remote interactive checks and authoritative verification |
| [Federated build cluster](specs/build-cluster.md) | Independently owned capacity through SSH bootstrap, collaborator enrollment, and managed pools |
| [Work assignment service](specs/work-assignment-service.md) | Implementation handoff for central selection, dispatch, continuation, and reconciliation |
| [Staging triage](specs/staging-triage.md) | Immediate fixes to the existing Bors path: no solo CI for isolated PRs, full staging builds, strip every attributed culprit per round |
| [AI merge queue](specs/ai-merge-queue.md) | Standalone scripted queue with attributed PR commits and AI conflict/CI repair |
| [Declaration collisions](specs/declaration-collisions.md) | Detect duplicate declarations between batch-mates at assembly, from module headers recorded at PR build time |
| [Review continuity](specs/review-continuity.md) | Implementation handoff for structured findings and approval reuse in the existing GitHub workflow |
| [Library integration service](specs/library-integration-service.md) | Implementation handoff for library registration, dependency snapshots, independent queues, and queue backends |
| [Evidence](evidence/README.md) | Measurement definitions, reproducible collection, samples, and limitations |

These are design and implementation specifications. Example endpoints and commands describe the proposed interface; they do not claim an existing deployment. Each component specification defines a first deployable increment, failure behavior, rollout, and acceptance scenarios. The AI merge queue and work assignment specifications are self-contained implementation tasks. The shared protocol is a proposal for components that explicitly adopt it, not a prerequisite for those standalone tasks.

## Decisions established by this discussion

1. Contributors remain able to use independently operated AI workers. Central coordination does not require central ownership of inference accounts.
2. Approval continuity is the default. Routine source movement does not revoke an approval or require a separate AI applicability assessment on every edit.
3. Successful builds certify their actual inputs. Reusing review judgment and reusing compiled evidence have different contracts.
4. Each integration unit has its own merge queue and configurable policy: conventional batching/splitting or scripted staging with AI conflict/CI repair and no separate staging review.
5. Subject libraries pin resolved dependency snapshots and advance independently. Every upstream merge must not force all downstream queues to restart.
6. In normal AI staging, acceptance transfers integration responsibility to staging. A green batch takes priority over any one contribution's intent: an agent that cannot make a contribution pass shrinks it, down to an empty commit, and the PR comment says so. When no inference path is available the queue holds and resumes automatically; there is no conventional-splitting fallback and no operator page.
7. GitHub becomes a publication mirror. Existing GitHub contributions continue during incremental deployment.
8. Deploy live, preserving normal worker and CI admissions. Component failures must not require draining the fleet.
9. Build a greenfield build service, learning from Kiln without depending on its implementation or deployment.
10. One logical build cluster must combine independently owned machines through SSH inventory, token-based one-line enrollment, and managed adapters such as Kubernetes. Owners retain resource and availability control.
11. Scripts perform clean insertions/rebases, control CI, and fast-forward publication. Agents resolve conflicts and source-dependent CI failures, preserving one attributed squash commit per PR and appending concise explanations only to commit messages.
12. Repair agents run on the worker fleet through the existing harness, authorized under the existing security-audit rule, with the failed commit's compiled artifacts restored before the agent starts. Running agents in GitHub Actions with a repository-secret API key is deferred.
13. The first queue increment is one open batch and one sealed head: approved PRs insert into the open batch by random draw within an age window of the oldest, the open batch seals when the head lands, the head is rebased, built, repaired in place and fast-forwarded, and there is no per-batch PR cap and no membership eviction. Successor batches with background CI, combination under a cap, cap-pressure sealing, agent-assisted insertion and clean-replay comment comparison are later increments adopted only on measured need. No batch is stacked on an unlanded predecessor and the open batch never runs CI.
14. The head build is the serial resource. Each candidate build records its runner, host and phase progress, and status raises a stall condition; a verdict comes only from the candidate's own completed run, so pull-request events and cancelled runs cannot reject a candidate or consume an approval; review and repair automation cannot withdraw a batch member; an operator urgent lane seals and builds next without preempting the head; the controller acts through its own GitHub App identity and reports its running revision.

## Immediate implementation packages

The first package is the [staging triage](specs/staging-triage.md): measured on 2026-11-03, the existing queue spends 28% of its staging slot on singleton runs for PRs that were never broken and needs about four CI rounds per landed large batch because `--fail-fast` exposes one culprit at a time. Its four changes are small, independently deployable and precede the queue redesign.

The next priority is the [standalone AI merge queue](specs/ai-merge-queue.md), using the existing PR, agent, and CI workflow, delivered in increments. Increment 0 reduces the central risk before any controller exists: a shadow repair task that runs the repair agent on real Bors failures without publishing, an attributed chain tool, readmission age for repaired suspects in Bors, a journal report, and the service's GitHub App. Increment 1 is a separate Python queue service with one open batch and one head, taking over opted-in repositories from Bors. There is no exceptional-withdrawal process: after staging admission, authors/reviewers address discovered problems in follow-up PRs to Tau Ceti.

Work assignment can start independently from its self-contained specification. The build service, review continuity adapter, and library registry can proceed on their own service boundaries. None requires the native contribution website or AI staging system to exist first.

The [migration DAG](migration-plan.md) names package boundaries and evidence required before expanding a rollout. The [machine-readable DAG](migration-dag.json) identifies hard prerequisites; a drawing's horizontal position does not imply an ordering requirement.

## Open design choices

- Decide the first subject library and its integration policy after examining import boundaries and recent overlap, rather than assigning illustrative subject names to production repositories.
- Learn from Kiln's measurements and failure modes while keeping implementation and cluster assembly independent. The build service does not require its adoption.
- Compare a native contribution interface with Gerrit as a review substrate. Neither choice blocks the early services.
- Set funded inference and build budgets, service availability objectives, and retained-data limits from measured costs. No new paid capacity is authorized by these specifications.
- Treat 2,000 added Lean lines as the proposed planning unit. Confirm whether the operator instead intends total churn or another LOC definition before setting size-based authoring incentives.

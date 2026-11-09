# AI merge queue specification

The production merge queue uses scripts to apply clean changes, construct attributed commit chains, schedule and interpret CI, and publish successful batches. Agents resolve merge conflicts and source-dependent CI failures, preserving the intent of the constituent PRs as best they can.

**Status, 2026-11-08:** the queue has owned production merging since November 4 and automatic admission since November 6. This document describes the current contract, the implemented extensions and the remaining work. The [implementation record](https://github.com/TauCetiProject/TauCetiWorker/blob/main/docs/queue/status.md) retains rollout history; the [operations runbook](https://github.com/TauCetiProject/TauCetiWorker/blob/main/docs/queue/operations.md) describes deployment. Read the running revision and effective settings from [live status](https://queue.taucetiproject.org/status), rather than inferring deployment from a merge.

Success means keeping up with incoming approved work while preserving CI, attribution and explanation correctness. Measure admissions, useful landings, outstanding work and its age over time. There is no fixed per-head duration target; the former 110-PR/two-failure/120-minute scenario is retired by operator decision on November 7. See [production monitoring](#production-monitoring).

## Delivery status

The original increments remain useful names for implementation history, not outstanding instructions to build or activate another queue.

| Area | Status |
| --- | --- |
| Increment 0 | Attributed chain tool, historical experiment harness, Bors readmission-age preservation, journal report and dedicated GitHub App implemented. The historical sample completed 9 candidates, all green, before a disk stop; the original 20-candidate sample was not completed. The operator authorized proceeding without it. [Results and limitations](https://github.com/TauCetiProject/TauCetiWorker/blob/main/docs/queue/repair-experiments.md). |
| Increment 1 | Controller, persistent state, GitHub adapters, worker repair task, fixtures, scratch rehearsal and production cutover implemented. The initial model was one open batch and one head. |
| Production extensions | Multiple unsealed batches (default limit 2), oldest-first sealed successor at 100 PRs, background preparation, speculative pre-rebase and preparation of that chain, streaming and parallel repair, scoped builds, scripted audits, repair-output reuse, admission diagnostics, author opt-out and public monitoring implemented. Their contracts are below. |
| Remaining work | Targeted compatibility and cleanup work, measurement coverage and optional later increments are distinguished below. Inference-free splitting and guaranteed reboot startup are postponed by operator decision on November 7. |

Background staging CI for sealed successor batches, batch combination and cap-pressure sealing, agent-assisted insertion, clean-replay comparison for comments and agents hosted in GitHub Actions remain optional later increments. Worker-side background preparation is already implemented; it does not supply a staging verdict.

## Queue and source model

Maintain a queue of approved PR revisions not yet inserted into a batch, up to `open_batch_limit` unsealed batches, a target of two sealed active batches total (including at most one head being validated). Each batch is a linear chain of single-parent commits based on a recorded canonical commit, containing exactly one squash commit per constituent PR, including an empty commit where appropriate. Open batches assemble and prepare on their own canonical bases. A speculative pre-rebase onto a validated head is retained separately until that head lands; it never establishes a staging verdict or stacks work on an unfinished repair.

Sealed membership is fixed except for the explicit author opt-out below. Unsealed batches receive approved PRs until they seal, subject to claims protecting active preparation. Only the oldest unsealed batch seals. While fewer than two active batches are sealed, counting the head, seal that batch once it has at least `sealed_successor_min_members` PRs (default 100; 0 disables new successor sealing). Sealing fixes membership and immediately enables worker preparation, bypassing the usual size and quiet gates. If promotion is waiting for an active preparation claim and no head exists, seal the next eligible batch too without interrupting that owner. Both sealed batches may temporarily remain non-head batches. The successor remains closed after preparation; new PRs use other unsealed capacity. A larger later batch never overtakes the oldest. Without a head, promote the oldest nonempty batch immediately, irrespective of this threshold and once its active claims release. Each batch records its claim state, source base/tip, and CI results. The sealed head can be claimed for repair.

Once a PR enters staging, the default is to land the AI's best effort at merging/fixing its content through the scripted CI and publication process. If the original author or a reviewer subsequently finds a problem, they address it in a follow-up PR to Tau Ceti. Such findings do not withdraw the staged contribution or reopen its admission review. Review, repair and housekeeping pipelines must honour published membership and must not close, relabel, re-review or withdraw a member. A member PR closed after staging admission still lands; closure is not withdrawal. The queue's `dont-repair-in-queue` policy is the explicit exception when integration would require editing an opted-out contribution; see [the author's choice](#repair-or-reject-the-authors-choice).

The head need not already be based on current `main` when it seals. The controller establishes that condition before promotion. Discover the configured canonical branch rather than assuming its name; Tau Ceti currently uses `main`.

There is no PR-count cap per batch. Process all approved waiting work instead of leaving it out to satisfy a preferred batch size. Multiple agent sessions may be required for a large batch; retain progress and complete inputs between sessions.

## Accepted inputs and attributed commits

Record the repository/PR, accepted head, description, author/coauthors, and existing admission evidence. Retain the Git objects independently of the contributor's mutable branch. Admission and insertion are retry-safe: one accepted revision cannot enter two active batches because an event or response was duplicated.

Each constituent commit includes stable source identity in its message, for example:

```text
Source-PR: TauCetiProject/TauCeti#123
Accepted-Head: <original accepted SHA>
```

Scripts create the initial squash commit and preserve its PR identity and attribution through rewriting. The controller validates single-parent history, the declared base, and a bijection between commits and recorded member PRs. Compare membership by PR identity, rather than requiring positional equality with a previous chain. Preserve empty commits explicitly during replay and validate the final member count; default Git behavior must not silently drop one.

Agents edit constituent commits instead of adding standalone repair commits. They receive original accepted sources, current commits, descriptions, prior messages, and actual conflict/CI evidence. An interaction fix can live in the most appropriate constituent commit and name the other affected PRs in its message. The attribution requirement preserves inspectable contribution identity; it does not require every accepted line or mutually incompatible implementation to survive.

A green batch takes priority over preserving any one contribution's intent. When an agent cannot make a constituent commit compile and pass CI within the batch, it shrinks that contribution to the part that does, down to an empty commit if necessary, and the appended message and the programmatic PR comment say what was dropped. Ordinary repair does not evict members and has no task time budget. Opted-out contributions instead follow the explicit withdrawal policy below. A member already landed on the new base is retained as an empty commit with a scripted note, separately from repair shrinkage.

The batch tip is the unit that must pass CI. Intermediate constituent commits need not each pass independently.

## Explanations and PR comments

An agent's explanations belong only in commit messages. For each constituent commit whose content it deliberately changes, it appends a very concise explanation of what changed and why, naming conflicting PRs where useful. Preserve the previous message text. Do not ask for a separate summary file, structured decision report, or second explanation through the queue API.

Agent instruction:

> Resolve the reported conflicts or CI failures in this batch. Preserve the constituent PRs' intent as best you can; where they conflict, use your judgment to choose the best alternative or combination. Update the constituent commits, retaining their source PR identities. When you change a commit's content, append a very concise explanation of what changed and why to its existing message. If a contribution cannot be made to pass, reduce it to the part that does, down to an empty commit, and say so in its message.

The controller records provenance per member commit: clean replay, or agent-edited in a named repair task. A mechanical rebase changing commit hashes does not by itself mean a PR's content was modified; replaying descendants after an earlier edit is not an edit to every descendant. A conflict requiring agent resolution is an edited contribution.

Check append-only message preservation against the corresponding prior commit by PR identity. Collect explanations from the appended message text. If an agent has changed source without the required message addition, have the same attempt add the concise explanation; no new review phase is introduced.

Include an edit explanation in the consolidated landing comment when the member's provenance is agent-edited. Reuse commit-message explanations directly, with links to the relevant retained commits and conflicting PRs, and show the diff against the accepted head in the PR's files. Apply `repaired-in-queue` to repaired contributions, distinguishing scripted already-landed empties. Comment construction/publication is programmatic. Do not add a mandatory post-hoc AI analysis to reconstruct decisions already made. Comparing the landed content with a clean replay of the accepted branch, to suppress edit notices that turned out to be no-ops, is a later increment.

Publish one concise consolidated comment per PR for the actual landed result. Failed comment publication is retried independently of source landing. Preserve modified commit objects under durable refs so comments and links remain usable after further rewrites. A separate decision store is unnecessary; operational records can identify inputs, outputs, and modified commits without duplicating the reasons in their messages.

## Scripted insertion

For each insertion pass, select an eligible PR from the `insertion_age_window` inputs ordered by urgency, priority and admission age. Draw urgent or overdue inputs first, otherwise draw at random; count passes spent in the window so repeated random draws cannot starve an eligible input. For the chosen PR, use a private workspace to attempt a clean replay/squash onto the selected open batch. Claim and recheck the batch's version before committing insertion. Concurrent attempts must also reserve the approved PR so it cannot be inserted twice.

Commit the new batch tip, membership, and removal from the waiting queue through a recoverable operation. A failed or interrupted probe leaves the approved PR represented and the existing batch intact. PRs need not form a strict global FIFO beyond the age window.

A PR that does not replay cleanly onto an open batch is tried against later open batches, creating another independently based batch if `open_batch_limit` allows. Otherwise it stays approved and waiting until a relevant base or tip moves. A PR whose head conflicts with `main` itself receives the ordinary conflict request. No agent is launched to insert a PR.

A clean textual replay is not enough for a PR that removes or renames a module: the deleted source may still be imported by an unchanged module on `main` or by a batch-mate. Before committing insertion, scan project imports in the prospective tree with `git grep`, confirming suspected missing sources by parsing their import headers and caching immutable tree evidence. Refuse newly introduced gaps with the ordinary missing-source request; existing gaps on the base are not attributed to this PR. Wait for a base refresh only when each missing source is absent from the probed batch's base and tip and is supplied by current `main` or introduced by another active batch. Another batch merely inheriting a source the PR removes is not an incoming dependency. If scanning is unavailable, record that fact and leave CI preflight authoritative. Declaration-name intersections at insertion produce repair diagnostics, not refusals; see [admission knowledge](#admission-knowledge).

An operator can mark an approved PR urgent; it receives priority within the insertion window without preempting or cancelling the running head. The sealed successor becomes head when the preceding head lands and its claims release; when no successor exists, the oldest nonempty unsealed batch becomes head. Separately, repository operations policy requires green bors/staging infrastructure changes to land by admin merge promptly. Administrative merges are reconciled as canonical advances, preserving accepted work.

If no open batch exists, create one when approved work is waiting. Remove an idle empty open batch rather than sealing it or running empty staging CI.

## Claims and candidate adoption

Use expiring renewable claims for scripted mutations and agent attempts. A claim records batch identity/version, starting tip, operation, worker/run identity, and a monotonically changing claim token. Take multiple claims in a consistent order or one transaction.

Workers use private worktrees/refs and return proposed commit chains. Only the controller adopts an authoritative batch tip after validating membership, history, expected batch version, and current claim. Expiry makes work eligible for recovery; it is not proof that the old process stopped. A late result cannot overwrite newer state, but its commits remain retained and can be reused.

Keep an active tool call running through coordinator/network failure. The harness preserves progress, retries communication, and reconciles on reconnect. Repeated loss of bookkeeping must not discard source or repeatedly restart the same model session. Actual publication still requires the necessary source/CI/ownership facts.

## Head operations and CI

The controller repeatedly reconciles the head:

1. When no head exists and the oldest open batch is nonempty, seal it as soon as its current claims are released. Replenish open capacity as needed. No collection window or additional insertion pass delays the seal. An unclaimed background offer never delays sealing; an active owner completes or releases its claim before sealing.
2. If the head is based on an older canonical commit, claim it and attempt a scripted rebase onto current `main`. On conflict, restore the previous authoritative series and make that rebase-conflict task available to an agent, with the failed probe's evidence retained.
3. Once it is based on current `main`, start or observe the required CI programmatically. Wait for a running check; recover infrastructure failures separately from source failures.
4. If source-dependent CI fails, make the repair task available to an agent. Adopt the updated series, then let the controller run CI on it again.
5. If required CI passes, fast-forward `main` from the recorded expected parent to the batch tip. Create no additional batch squash or merge commit. If the expected parent changed, return to rebase/validation. GitHub does not classify a PR as merged when its squash commit lands this way; PRs close by reference, as with Bors today, and landing evidence stays positive through the `Source-PR` trailer and retained refs.

CI scheduling, result classification, retry, and promotion are controller operations. Agents can use local verification while editing, but do not decide whether required CI ran or which checks permit publication. A CI result is bound to the actual candidate, run and checking configuration; an old callback cannot mark a new source green, and a cancelled or superseded run cannot mark a candidate red. The controller owns the dispatch and the concurrency of candidate builds: a pull-request event, a push to a member branch, or a workflow's always-run reporting step must not cancel a candidate build or post a verdict in its place, and no status that did not come from the candidate's own completed run is read as its verdict. An approval is never consumed by a stale or cancelled status. Source-dependent checks and cancelled/lost runners or missing status publication take different recovery paths.

Keep accepted membership and useful source during admin merges or queue restarts. A canonical advance does not reset original PR review.

A completed run whose bound `build` status has not arrived receives the same ten-minute publication grace as missing companion verdicts. Freeze the observed completion timestamp per run/attempt, persisting it across restarts; polls and callbacks cannot extend the deadline. Normal polling intervals still apply, and status webhooks permit prompt observation. A finalizer failure, cancellation or timeout bypasses the grace; an unrelated job's cancellation does not prove publication failed. Late bound verdicts settle the original attempt without another dispatch. This behavior and the inherited-source wait fix are implemented in [PR 607](https://github.com/TauCetiProject/TauCetiWorker/pull/607).

## Staging capacity and build progress

The head build is the serial resource. Staging builds use the normal configured CI runner routing; the controller records which runner and host executed each candidate build and how long each build phase took, so placement decisions can be made from measurements.

Status reports CI phase progress and timestamps and raises a visible stall condition after `stall_interval_seconds` without observed progress. Public repair telemetry additionally samples Lake's completed/total build tasks, last increase and worker liveness; those counts are not module counts or CI verdicts. Operators must be able to answer "which machine is this running on and is it moving" without access to the runner host or private logs. Unknown progress remains unknown; a heartbeat does not prove build progress.

The CI contract for a candidate is the one established by the staging triage work: the build runs to completion and reports every independently failing module, and audit and lint findings carry the defining module path. The repair agent's inputs depend on that contract.

## Runtime controls

Expose these runtime controls through the existing administration surface or protected service settings, without a code PR. Apply changes to new scheduling decisions and report effective values. Values below are defaults; live status is authoritative for production overrides. Configuration files seed the store rather than overwriting runtime choices on restart.

| Setting | Meaning |
| --- | --- |
| `insertion_age_window` | Size of the eligible insertion window ordered by urgency, priority and age; default 16 |
| `open_batch_limit` | Maximum open batches, each assembling on a canonical base; default 2, with 1 restoring the original increment 1 shape |
| `repair_concurrency` | Maximum live claims across distinct tasks; each task has one owner. A second worker prepares the next eligible batch or waits, never races the head's owner. Background claims reserve a slot for head work; default 1, which disables background claims |
| `stall_interval_seconds` | Lack of observed progress after which status raises a stall condition; default 900 |

## Repair agent execution

Conflict and repair agents run on the worker fleet through the existing `TauCetiWorker` harness. The implemented provider chain supports Claude subscription access, Codex and API inference, with pacing and fallback. The queue exposes a named claimed task; a worker claims it, receives the normal source and diagnostics, and returns a proposed attributed chain. Its output is never permission to bypass the subsequent scripted CI. The historical experiment task uses the same chain model without adoption or publication.

Authorize worker execution using the existing security-audit rule requiring repository-admin permission, verifying the selected GitHub account at claim and result adoption. Identity caching supports rate limits and restarts. A PR comment or local declaration of membership is not authorization.

The agent must have warm build artifacts. The historical experiments found the published `main` cache at the base plus an incremental build sufficient; failed historical candidates had no published outputs. Production additionally restores repair outputs from relevant candidate tips. Conflict work fetches cache artifacts for the rebase target without first building the obsolete chain. A task whose artifacts cannot be prepared is reported as such; it is not silently run cold. The cache and verification contract is specified below.

Every execution path shares the same claims. A delayed or duplicate attempt cannot create a competing authoritative writer.

## Inference unavailability

**Postponed (operator, 2026-11-07):** inference-free splitting within this queue engine remains a desired future capability. Its design and implementation are unscheduled; do not fall back to the legacy Bors engine. Hold-and-retry remains the current operating policy until this work is resumed.

When no execution path can obtain inference (provider errors or exhausted credentials across every configured path, after bounded retries), the queue holds: it stops sealing, the head waits with its membership and retained source intact, and blocked repair tasks remain exposed to any authorized worker that can still run. The controller retries with backoff and resumes automatically when a task completes or a provider call succeeds. There is no conventional-splitting fallback, no return of members to authors, and no operator page. A difficult source problem or an ordinary infrastructure error is not evidence that inference is unavailable.

Queue status reports the hold, its start time, and the last provider error so the cause is visible after the fact.

## State and implementation

The queue is the `tauceti_queue` Python package in `TauCetiProject/TauCetiWorker`, with its own entrypoint, tests under `tests/` and operational documentation under `docs/queue/`. It runs separately from Bors and uses its own GitHub App installation and API budget. Webhooks and bounded fallback polling observe GitHub state. Source, records and GitHub side effects reconcile after restart because they cannot share one transaction.

The single-process service uses one SQLite database, replacing the original PostgreSQL proposal. It records:

- approved inputs: repository, PR, accepted head, description, authors, admission evidence and retained source;
- batches: identity, version, base/tip, ordered members, provenance, claims, CI attempts, placement and timings, plus separately retained speculative preparation;
- claims: batch/version, operation, worker identity, token and expiry;
- tasks, proposed chains and pending side effects, with idempotency keys and attempt history.

Explanations remain in commit messages. Monitoring and journal records describe operations and measurements without duplicating the agent's reasons.

Automatic admission is owned by the service, using the pinned pure TauCetiReview policy over published reviews, current source and trusted build/scope/recovery evidence. Durable pending evaluations survive resource waits and restarts. Legacy automatic admission workflows were retired at the November 6 admission cutover; late legacy eligibility publications have no authority in policy mode. Admission itself requires no inference.

The compatibility surface retains Bors commands (`bors merge`, `bors r+`, `bors r=login`, `bors p=N`, `bors r-`, `bors cancel`) from authorized actors, membership labels, statuses, idempotent comments and the `tauceti-bors.observation/v1` and `tauceti-bors.candidates/v1` feeds at the established paths. Cancellation commands do not withdraw staged members. Candidate dispatch uses `repository_dispatch` of `pr-build.yml` with immutable source and the staging-candidate identity expected by production CI. Promotion is a fast-forward push. See the [adapter contract](https://github.com/TauCetiProject/TauCetiWorker/blob/main/docs/queue/github.md).

The versioned `/status` JSON endpoint exposes queue/head state, effective settings, claims, source, tasks, CI placement/progress, waiting reasons, running revision and service start time. Local fixtures use real Git history and scripted providers with no paid inference. Live validation uses natural authorized work and the scratch rehearsal repository.

## Policy status

Current policy is one sealed head, a bounded number of independently assembled open batches, immediate sealing after claims release, scripted insertion, worker-side repair, candidate-bound staging verdicts and fast-forward publication. Background preparation and speculative pre-rebase reduce later work but never authorize landing. Each task has one live worker owner; independent agents within that task may repair disjoint groups under that owner's orchestration.

Ordinary members are retained through repair, including shrink-to-empty, and are protected from other automation. The explicit author opt-out is the exception. Explanations, edit diffs and durable commit links are published programmatically. Hold-and-retry remains the inference-unavailability policy. Optional later increments and postponed operations are listed separately below.

## Acceptance and regression scenarios

These are regression requirements, not a claim that every scenario has complete production evidence. Historical sampling and cutover requirements are identified explicitly. The [controller fixture mapping](https://github.com/TauCetiProject/TauCetiWorker/blob/main/docs/queue/controller.md) and [pilot report](https://github.com/TauCetiProject/TauCetiWorker/blob/main/docs/queue/pilot-2026-11-05.md) retain the available evidence and its limits.

1. Clean insertion, batch rebase, CI launch/observation, and publication complete with zero agent invocations.
2. A conflict or source-dependent CI failure produces a narrowly identified AI task and a validated replacement chain; there is no staging review or diagnostic bisection/singleton CI. Natural one-member heads are allowed.
3. Every final commit is single-parent and maps bijectively to a member PR, including empty contributions. Validation accepts a membership-equivalent replacement chain without comparing member positions.
4. Agent edits preserve each previous message as a prefix and append concise reasons. Comments are generated from those messages; no extra agent decision file/report is required.
5. A mechanical rebase does not create a content-modification comment, while agent-edited contributions get their final linked comment with independently retryable publication.
6. PR reservation, claims, claim expiry and late results cannot duplicate or lose members or overwrite a newer candidate.
7. Sealing, promotion and restart preserve at most `open_batch_limit` open batches and one head. A claim protects the source it covers; insertion may append beyond a speculative task's recorded prefix, but cannot overwrite it. Active preparation retains ownership until completion or release; sealing has no artificial collection delay.
8. Open batches never dispatch staging CI. Worker-side preparation can build and repair them without supplying a verdict. Settings can change without a PR.
9. A PR that does not replay cleanly onto the open batch is probed again after the head lands and inserts without an agent once its batch-mate has landed.
10. Admin advances and lost promotion responses reconcile correctly; promotion adds no merge commit, and stale CI cannot approve changed source.
11. An authorized worker claims a repair task, prepares warm artifacts before the agent starts, and returns a chain the controller validates; a second claimant or a late result cannot become a competing writer.
12. Coordinator outages preserve active tool calls and useful source; restart recovers actual worker/CI/publication state before repeating work.
13. When every inference path is unavailable, the queue stops sealing and retains repair membership and source, reports the hold and its cause in status, and resumes automatically on the first successful task or provider call. No member is evicted because inference is unavailable. Inference-free splitting is postponed.
14. An ordinary contribution the agent cannot make pass is shrunk, down to an empty commit, and the batch still lands; its PR comment names what was dropped. An opted-out contribution follows `problem-anticipated` withdrawal instead. No repair task is abandoned for lack of a time budget.
15. Live activation retains useful source and declared ownership of every pending batch/member.
16. A problem reported by an author or reviewer after staging admission does not withdraw the contribution or reopen admission review. The batch lands the AI's best-effort integration after required CI; corrections are separate follow-up PRs to Tau Ceti.
17. Over representative production windows, the queue keeps up with incoming approved work: arrivals and landings reconcile with outstanding work, inherited backlog and age are accounted for, and sustained backlog growth or ageing is explained and addressed. Report nonempty landings, shrinkage and withdrawals separately; dropping content does not establish sufficient useful throughput. There is no fixed head-duration or CI-round limit. See the measurement contract below.
18. Age-windowed insertion includes an overdue-input safeguard; eligible work is not starved by repeated random draws. Report conflicts, missing-source waits and resource holds separately from draw fairness.
19. A pull-request event or branch push during a candidate build neither cancels that build nor posts a verdict for it; a cancelled or superseded run leaves the candidate's state unchanged and its approval intact.
20. An urgent PR is prioritized within the insertion window without cancelling the running head or bypassing the oldest-open-batch sealing order. The separate operator policy for admin-merging green bors/staging infrastructure changes still applies.
21. A review or repair pipeline closing or relabelling a member PR does not change batch membership; the PR's content still lands.
22. Queue status names the runner and host of the running head build, shows phase progress with timestamps, and raises a stall condition when progress stops; it also reports the service's running revision.
23. Historical evidence: the experiment harness reports time to green, edited PRs, shrink events and cost without publishing source; 9 candidates completed rather than the originally requested 20. Bors readmission-age preservation was implemented and verified before cutover. The operator waived completing the sample as a prerequisite to production.
24. Transition evidence: the scratch rehearsal passed; the operator waived the two-day shadow gate. Production ownership and accepted work were transferred with retained source and original ages through the actual cutover described below.
25. When the insertion scan is available, a PR introducing an import with no source is refused before head CI, or waits if all missing sources are in flight. A scan failure is recorded as unknown and CI preflight remains authoritative.
26. A predicted compiled declaration collision reaches a preparation or head repair task without refusing admission. A speculative result cannot supersede a newer chain or establish a CI verdict; a same-tip result does not restart CI.
27. An opted-out contribution is not repaired in place: required changes produce an attributed empty placeholder and `problem-anticipated` withdrawal. When the interaction becomes reproducible, the PR receives `needs-rebase`; if the interacting work leaves without landing, readmission retains the original age.

## Implementation and cutover record

The original implementation streams are complete: attributed chain tool, controller/store/fixtures, GitHub adapters and compatible feeds, repair runner, deployment tooling and scratch rehearsal. The historical experiment harness and Bors baseline report are implemented; the experiment sample remains partial. Their PRs, results and incidents are linked from the [implementation record](https://github.com/TauCetiProject/TauCetiWorker/blob/main/docs/queue/status.md). These streams are not standing instructions to launch new agents, recreate the App or repeat a production cutover.

The operator authorized proceeding on November 4 without the full historical sample or two-day shadow gate. The scratch rehearsal was retained and passed. The production cutover imported approved work and original ages, suspended the old Bors App, enabled queue publication and moved `bors.taucetiproject.org` to the new service. `MERGE_BACKEND=bors` was retained for compatibility; activation was not the originally proposed single repository-variable switch. Service-owned automatic admission followed on November 6, retiring the legacy admission workflows.

Deployment remains live: keep normal worker and CI admissions enabled, preserve useful source and active task ownership, verify running revisions and actual completions, and repair interrupted or stranded work. Do not routinely drain the fleet. Use the [operations runbook](https://github.com/TauCetiProject/TauCetiWorker/blob/main/docs/queue/operations.md) for backup, migration, deployment and restoration; historical status entries are not current deployment instructions.

## Production monitoring

The success criterion is sustained capacity to keep up with incoming approved work, with correct source, attribution and explanations. No fixed batch size, head-duration ceiling or prescribed number of CI rounds determines success. Head duration and repair rounds remain diagnostic measurements, since they explain capacity and waiting time.

For daily, rolling seven-day and whole-pilot windows, report:

- approved arrivals, unique PR identities landed, outstanding approved work across waiting inputs/open batches/head, and the change in that outstanding work;
- nonempty contributions landed, already-landed empty placeholders, repair reductions (including partial shrink where measurable), opt-out withdrawals and re-admissions, separately from PR closures or member occurrences;
- backlog size and age over time, including insertion waits and time already spent inside batches, with explicit reasons for blocked work;
- CI and repair time, overlap, rounds, repeated or superseded work, claim collisions/expiry, inference holds and observed stalls;
- recorded inference cost and token subtotals with missing-attempt coverage, plus prediction/repair outcomes and comment/attribution audits.

Keeping up means outstanding work and its age do not grow persistently under ordinary inflow; if an inherited backlog is present, show its trajectory separately. While a backlog exists, compare landing rate against approved arrival rate over the same window, including downtime. Once caught up, equal arrival and landing rates with bounded waiting is success; landing more than arrives is not a continuing requirement. Explain burst windows and long-lived blocked inputs rather than hiding them in averages. Count new PR identities and accepted revisions separately where re-approval would otherwise double-count demand, and reconcile withdrawals and re-admissions so they cannot masquerade as landings. Shrinking or withdrawing contributions is reported explicitly and cannot by itself establish that useful work is keeping up.

The existing journal/pilot calculations and hourly public history provide much of this evidence; complete arrival/backlog reconciliation and age coverage still need verification. Missing measurements remain unknown. The Bors triage baseline is historical context, not the acceptance threshold. The first-day pilot's 1,502 unique PR landings and 1,401 nonempty contributions included an inherited backlog and do not establish sustained capacity. Its 494-minute large-head cycle is diagnostic history, not failure against a retained duration target. See the [pilot report](https://github.com/TauCetiProject/TauCetiWorker/blob/main/docs/queue/pilot-2026-11-05.md) for costs, coverage and attribution limits.

Status and the public monitoring view expose waiting reasons and conditions. Stalls beyond two stall intervals, inference holds beyond an hour, repeated chain-validation failures, non-racing promotion failures and feed divergence must be visible to the next operator, without adding an automatic page or treating an alert alone as authorization to cancel useful work. If sustained backlog growth or ageing shows the queue cannot keep up, use the measurements to identify and address the limiting resource. Artificially withholding normal admissions to make the queue look caught up does not satisfy this criterion.

## Implemented repair and cache improvements

The November 5 cutover follow-ups are implemented. Their current contracts replace the earlier “implement now” list:

| Capability | Contract |
| --- | --- |
| Ownership and declaration context | Precompute member file ownership and added/removed/renamed declarations in `.tauceti-repair/files.jsonl` and `decls.jsonl`. Accepted compiled added-name records improve clean-member context; diff-derived names and rename ripples remain approximate navigation. |
| Streaming repair | A worker-owned background build appends located findings to `errors.jsonl` as they arrive. The lead agent can start before the pass finishes; scoped builds wait for the shared lock. Parallel grouping waits for the complete build findings. Exactly one build runs at a time within a task. |
| Parallel repair | Independent groups run agents in separate worktrees under the task's single owner. Fold fixups into constituent commits, preserving empty attributed members; verify the combined chain before submission. A lead agent handles remaining interactions. |
| Scoped builds | `.tauceti-repair/build <Module> ...` builds named modules, with optional dependents, under the same lock. Local scoped success is not a publication verdict. |
| Scripted checks | The worker runs the trusted pinned import/duplicate audits and changed-module environment lint after green builds, including streaming and final verification. Findings are ordinary diagnostics; the agent cannot dismiss them or authorize submission with unresolved findings. |
| Sandbox and pressure recovery | Worker builds use CI's offline `bwrap` sandbox and the trusted compiler pressure gate with capped cgroup ancestors visible. Separate repair-worker user services recover process/OOM failures and resume claims/sessions while the user manager is running. |
| Repair outputs | Publish outputs of the verified tip to `tauceti-repair` after submission so upload does not block adoption. CI may consume those outputs, then runs its build, audits and lint as the required verdict. The daily from-scratch verification remains an independent check. |
| Reuse across replays | Dispatch `repair_outputs_revs` containing relevant current/previous batch tips, landed-head tips and bases. Fetch available mappings and deduplicate artifacts; Lake validates content traces. This resolves the earlier concern that replayed commit SHAs miss the verified tip's outputs; a new tree-hash cache scheme is not pending work. |
| Exact base lookup | Dispatch `LAKE_CACHE_BASE_REV` for the public base cache rather than walking a bounded number of constituent commits. Repair-output fetch still runs if the public base mapping is absent. |

A `lean4checker` replay over restored outputs remains an optional future tightening, not a current CI requirement.

The experiments support warm base artifacts plus an incremental build, but do not isolate the wall-clock benefit of streaming, parallelism or speculative preparation. Measure those savings and repeated work from production outcomes; implemented does not mean their performance benefit has been established independently.

## Admission knowledge

The earlier “Bors knowledge now unused” follow-up is implemented. The service retains accepted-head-bound `DECLARATION-NAMES` records from trusted PR builds, and insertion attempts retain replay conflicts and path ownership. Exact added-name intersections produce predicted collisions without refusing a PR. Shared paths and import components accompany task context as navigation; overlap alone is not a source failure. The Bors batcher inventory was completed; its offline diversity planner was not another production admission predicate.

Predicted compiled collisions can trigger open-batch preparation ahead of the usual size/quiet thresholds or a head repair alongside its first CI attempt. The candidate's CI continues; a passing candidate can retire speculative work and land. A changed adopted chain needs its own CI. A same-tip speculative submission does not change the batch version or restart CI. Reports distinguish predicted names, duplicates actually observed by scripted checks, verified fixes and whether they preceded the actual CI verdict. Incomplete evidence stays unknown. See [admission knowledge](https://github.com/TauCetiProject/TauCetiWorker/blob/main/docs/queue/admission-knowledge.md) for bounds, provenance and recovery details.

## Pipeline acceleration behind the head

Implemented on November 5:

1. **Preparation on a non-head batch's own base.** The sealed successor is immediately eligible; unsealed eligible quiet batches are built and repaired by a worker, without a staging dispatch. Predicted collisions can make smaller batches eligible. A task has one owner; head work has priority, and background claims leave capacity for head work under `repair_concurrency`. Active preparation protects its batch, while insertion uses later open capacity or waits.
2. **Speculative pre-rebase.** Once the head has a validated chain awaiting CI, replay the next batch onto that tip and retain the result separately from its authoritative own-base chain. Conflicts and predicted interactions reach its worker. Redo or supersede speculative work when the head changes; do not stack on an unfinished head repair.
3. **Preparation of the pre-rebased chain.** Build, repair, audit and publish outputs for that speculative chain, warming from relevant head/batch repair tips. If the head lands at the expected tip, sealing can reuse the chain. Unclaimed offers never delay sealing; active claims finish or release first. Insertion beyond the speculative prefix is reconciled before reuse.

Added November 8: keep two sealed active batches total, including the head, selecting oldest unsealed batches at 100 PRs. Replenish this total even during a headless handoff waiting for an active preparation claim. The sealed successor keeps its background claims and worker protocol, does not consume `open_batch_limit`, and remains closed through preparation and restart. Head creation is immediate even below 100 PRs; promotion reuses applicable preparation/pre-rebase work and replenishes the successor.

A green worker build is never the staging verdict. Only the head runs staging CI; the sealed successor uses worker preparation, with fresh authoritative CI required for the final promoted candidate.

## Repair or reject: the author's choice

Implemented on November 5. Repair in place is the default because most observed repairs integrate interactions an author cannot yet reproduce against `main`.

- **Tell the author what changed.** The consolidated landing comment includes appended explanations, durable commit links and a diff against the accepted head in the PR's files. Each landed contribution retains `Source-PR:`, `Accepted-Head:` and its previous message prefix.
- **Explicit opt-out.** A PR admitted with `dont-repair-in-queue` may replay cleanly, but its contribution is not repaired in place. If it needs changes, the agent empties its constituent commit with an explanation and the queue withdraws it into `problem-anticipated`. The comment names known interacting PRs, files and findings and explains when the issue is not yet reproducible. Approval is not reused. This is the explicit exception to ordinary member retention, not permission for external automation to withdraw members.
- **Make the finding actionable.** When the interacting PR lands, apply `needs-rebase` and comment that the problem is reproducible; ordinary re-approval is required. If the interacting work leaves without landing, readmit at the original age with a comment.

Count opt-outs, repairs and withdrawals in reporting. A per-author opt-out or a different default remains a future policy decision, not an implemented requirement.

## Public monitoring

The read-only operational view is implemented at [taucetiproject.org/ci/](https://taucetiproject.org/ci/#queue), rather than waiting for a separate site on the queue hostname. It provides queue/batch views, PR lookup, CI/repair timelines, worker/build observations, recorded usage and cost coverage, and retained history alongside runner activity.

A separate read-only sidecar projects explicit public fields from service state and worker checkpoints/outcomes into the public CI data service. Hourly history reuses pilot reporting. Public browsers do not read the queue database or private task payloads; monitoring failure does not block admissions or repair. Source freshness, retained detail limits, partial coverage and missing costs remain explicit. Worker liveness is distinct from build progress, and current status alone cannot establish historical stall-free operation. See the [monitoring contract](https://github.com/TauCetiProject/TauCetiWorker/blob/main/docs/queue/monitoring.md).

The old batch-cell strip and other presentation ideas are optional refinements. Do not treat every item in the original sketch as an unfinished feature or claim complete decision, tool-call or spend coverage merely because a timeline is visible.

## Remaining work

These items warrant targeted follow-up; this specification cleanup does not implement or deploy them:

- Reconcile approved inflow, outstanding work and useful landings over representative windows, including backlog age, re-admissions, withdrawals and missing measurement coverage. Continue attribution/comment audits; compare optimization costs and savings using actual outcomes.
- Recheck consumers of the missing `tauceti-merge-reconcile` and `tauceti-bors-pr-approved` dispatches, and restore any still-needed focused reconciliation. Some consumers have changed since cutover; confirm the current need before restoring historical events.
- Delete obsolete `staging-candidate-*` branches after their attempts are terminal and no longer needed by CI identity checks. Preserve durable source refs and commit links. Superseded-run cancellation itself is already implemented.

### Postponed work

**Operator decision, 2026-11-07:** inference-free splitting and guaranteed reboot startup remain postponed. The subsequently authorized host relocation completed on November 8; it does not complete reboot supervision.

Hold-and-retry remains the inference policy. Splitting, when resumed, must stay within this engine and specify partitioning, attribution, ordering and recovery without returning control to legacy Bors.

The controller, tunnel, repair workers, monitoring and backups now run through systemd user units on chungus2. Repair execution moved on November 7 and the controller moved on November 8, retaining the running revision and queue state. Crash/OOM recovery while the user manager is running is implemented, but automatic startup after reboot or user-manager loss is not guaranteed (`Linger=no`). The [runbook](../../queue/operations.md#current-deployment) records the deployment and its limitation. Guaranteed reboot recovery remains unscheduled.

## Later increments

These optional, unimplemented extensions are retained for adoption when production measurements show a need. They do not change current behavior above or activate the postponed work.

### Background CI for sealed successor batches

The implemented queue already has a sealed prefix (two active batches total, including at most one head) followed by an unsealed suffix, with `open_batch_limit` initially 2 and oldest-first successor sealing at 100 PRs. An optional extension would allow more sealed successors and background staging CI. Sealed non-head batches could run CI and receive agent repair before reaching the head, controlled by a runtime flag; background results apply to the source tested, and after a new base is integrated the candidate needs its own results. Background work operates on independently based batches and proactively rebases them toward current `main`, coalescing repeated advances into a pending refresh. Head repair and validation take priority over background work. A CI backlog threshold (initial value 100 queued jobs on the runner pools used by staging, counting jobs rather than PRs, with zero assumed when the observation is stale or unavailable and the assumption reported) permits new background launches; it never stops the head's necessary build. When the head lands, promote the successor and replenish the sealed prefix as capacity permits; prior background work reduces preparation time but does not establish the final candidate's green status. Only the first unsealed batch may be sealed, preserving the sealed-prefix invariant; a borrowed batch is marked to seal on release without preempting the current attempt.

### Combination and cap pressure

Unsealed batches are combined programmatically only when the queue is up against its unsealed cap: borrow both without deadlock, attempt a clean combination of their commit series, and adopt the result atomically, preserving membership, attribution and messages; on conflict leave both intact and launch no agent. When the cap is reached and combination cannot free a slot, seal the first unsealed batch even beyond the sealed target, logging batch identity, sealed/unsealed counts before and after, effective limits, the backlog observation and the reason combination failed, with a distinct counter for target overflow exposed in status. Capacity pressure must not make approved work disappear or create an unlimited number of empty branches.

### Agent-assisted insertion

If an insertion cannot be made cleanly on any available batch, an agent can borrow one unsealed batch and incorporate the PR as one additional constituent commit, editing other constituent commits where needed with append-only explanations. A borrowed insertion reserves that PR until completion or recovery.

### Clean-replay comparison for comments

Include an edit notice in the ordinary landing comment only when the final contribution differs from the clean result of rebasing/squashing its accepted branch onto its actual predecessor, so that an agent edit that turns out to be a no-op produces no edit notice. When a clean replay baseline cannot be produced, retain the agent-edit provenance rather than pretending to establish an exact comparison.

### Agents in GitHub Actions

Running agents as GitHub Actions jobs with an API key stored as a repository secret: that job holds a credential while reading PR-authored text and running tools, so it needs an egress allowlist limited to the model API and GitHub, a spend-capped key, and the stated assumption that PR text is untrusted input. Credentials and trusted workflow code remain outside candidate-controlled builds; ordinary source checks keep their existing isolation. A worker and an Actions job share the same claims.

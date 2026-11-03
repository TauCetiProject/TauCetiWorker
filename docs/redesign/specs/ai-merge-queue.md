# AI merge queue implementation plan

Implement a scripted merge queue with AI assistance for merge conflicts and source-dependent CI failures. Scripts apply clean changes, construct attributed commit chains, schedule and interpret CI, and publish successful batches. Agents resolve conflicts and fix failing source using their judgment, preserving the intent of the constituent PRs as best they can.

This is a standalone plan for the existing GitHub PR, agent, and CI workflow. All required behavior is described here. Examples describe interfaces to implement.

## Queue and source model

Maintain a queue of approved PR revisions not yet inserted into a batch, and an ordered queue of batches. Each batch is a linear chain of single-parent commits based on a recorded canonical commit. Batches are independent branches, rather than descendants of earlier batches. A batch is never stacked on an unlanded predecessor: a red result on a stacked candidate cannot be assigned to the predecessor or the successor without another build, so it is not a repair task, and concurrent repair of both would invalidate each other's work. The cost is that a successor's background CI only finds its own failures early; it never shortens the head's critical path. Each contains exactly one squash commit per constituent PR, including an empty commit where appropriate.

The batch queue consists of a sealed prefix followed by an unsealed suffix. Membership is not evicted during normal operation; sealed membership is fixed, while unsealed batches can receive approved PRs. Each batch separately records its borrow/claim state, source base/tip, and CI results. A sealed batch can be borrowed for repair; borrowing does not itself seal it.

Once a PR enters staging, land the AI's best effort at merging/fixing its content through the scripted CI and publication process. There is no exceptional-withdrawal process. If the original author or a reviewer subsequently finds a problem, they address it in a follow-up PR to Tau Ceti. Such findings do not withdraw the staged contribution or reopen its admission review.

The head need not already be based on current `main` when it reaches the front. The controller establishes that condition before promotion. Discover the configured canonical branch rather than assuming its name; Tau Ceti currently uses `main`.

There is no PR-count cap per batch. Process all approved waiting work as capacity permits instead of leaving it out to satisfy a preferred batch size. Limit the number of unsealed batches through runtime configuration. Multiple agent sessions may be required for a large batch; retain progress and complete inputs between sessions.

## Accepted inputs and attributed commits

Record the repository/PR, accepted head, description, author/coauthors, and existing admission evidence. Retain the Git objects independently of the contributor's mutable branch. Admission and insertion are retry-safe: one accepted revision cannot enter two active batches because an event or response was duplicated.

Each constituent commit includes stable source identity in its message, for example:

```text
Source-PR: TauCetiProject/TauCeti#123
Accepted-Head: <original accepted SHA>
```

Scripts create the initial squash commit and preserve its PR identity and attribution through rewriting. The controller validates single-parent history, the declared base, and a bijection between commits and recorded member PRs. Compare membership by PR identity, rather than requiring positional equality with a previous chain. Preserve empty commits explicitly during replay and validate the final member count; default Git behavior must not silently drop one.

Agents edit constituent commits instead of adding standalone repair commits. They receive original accepted sources, current commits, descriptions, prior messages, and actual conflict/CI evidence. An interaction fix can live in the most appropriate constituent commit and name the other affected PRs in its message. The attribution requirement preserves inspectable contribution identity; it does not require every accepted line or mutually incompatible implementation to survive.

A green batch takes priority over preserving any one contribution's intent. When an agent cannot make a constituent commit compile and pass CI within the batch, it shrinks that contribution to the part that does, down to an empty commit if necessary, and the appended message and the programmatic PR comment say what was dropped. This is the only exit from a repair task. Membership is never evicted and no repair has a time budget, because the agent can always reach green.

Initially the batch tip is the unit that must pass CI. Intermediate constituent commits need not each pass independently.

## Explanations and PR comments

An agent's explanations belong only in commit messages. For each constituent commit whose content it deliberately changes, it appends a very concise explanation of what changed and why, naming conflicting PRs where useful. Preserve the previous message text. Do not ask for a separate summary file, structured decision report, or second explanation through the queue API.

Agent instruction:

> Resolve the reported conflicts or CI failures in this batch. Preserve the constituent PRs' intent as best you can; where they conflict, use your judgment to choose the best alternative or combination. Update the constituent commits, retaining their source PR identities. When you change a commit's content, append a very concise explanation of what changed and why to its existing message. If a contribution cannot be made to pass, reduce it to the part that does, down to an empty commit, and say so in its message.

A mechanical rebase changing commit hashes does not by itself mean a PR's content was modified. The controller records machine-derived provenance for clean replay versus agent-edited content and compares candidate content with the clean scripted replay where that baseline exists. A conflict requiring agent resolution is an edited contribution. Replaying descendants after an earlier edit is not automatically an edit to every descendant.

Check append-only message preservation against the corresponding prior commit by PR identity. Collect explanations from the appended message text. If an agent has changed source without the required message addition, have the same attempt add the concise explanation; no new review phase is introduced.

Post a PR comment when its final contribution differs from the clean result of rebasing/squashing its accepted branch onto its actual predecessor. Reuse commit-message explanations directly, with links to the relevant retained commits and conflicting PRs. Comment construction/publication is programmatic. Do not add a mandatory post-hoc AI analysis to reconstruct decisions already made. When a clean replay baseline cannot be produced, retain the conflict/agent-edit provenance rather than pretending to establish an exact clean comparison.

Publish one concise consolidated comment per PR for the actual landed result. Failed comment publication is retried independently of source landing. Preserve modified commit objects under durable refs so comments and fallback links remain usable after further rewrites. A separate decision store is unnecessary; operational records can identify inputs, outputs, and modified commits without duplicating the reasons in their messages.

## Scripted insertion and combination

For each insertion pass, draw the next PR at random from the `insertion_age_window` oldest approved PRs, and randomize the order in which eligible unsealed batches are tried. The age window prevents starvation; within it, randomness spreads conflicts across batches. Skip borrowed batches. For a chosen PR, use a private workspace to attempt a clean replay/squash onto a candidate batch. Borrow and recheck that batch's version before committing the insertion. Concurrent attempts must also reserve the approved PR so it cannot be inserted twice.

Commit the new batch tip, membership, and removal from the waiting queue through a recoverable operation. A failed or interrupted probe leaves the approved PR represented and the existing batch intact. PRs need not form a strict global FIFO beyond the age window.

If no unsealed batch exists, create an empty one when approved work is waiting. When all existing unsealed batches are borrowed, another may be created below the configured cap. If an insertion cannot be made cleanly on any available batch, an agent can borrow one unsealed batch and incorporate the PR as one additional constituent commit. It can edit other constituent commits where needed, with append-only explanations. A borrowed insertion reserves that PR until completion or recovery.

Unsealed batches are combined programmatically only when the queue is up against its unsealed-batch cap. Borrow both batches without deadlock, attempt to combine their commit series cleanly, and adopt the combined result atomically. Preserve membership, attribution, and messages. If the combination conflicts, leave both original batches intact and available; do not launch an agent merely to combine batches. Claims and record updates must prevent partially completing a combination or duplicating its members.

When the unsealed cap is reached and lazy clean combination cannot free a slot, seal the first unsealed batch when it is nonempty and available, even if this exceeds the normal sealed target. This frees an unsealed slot for further work. If that batch is borrowed, mark it to seal as soon as the borrow returns. Capacity pressure must not make approved work disappear or create an unlimited number of empty branches.

Clearly log every cap-pressure sealing operation, including batch identity, sealed/unsealed counts before and after, effective limits, current backlog observation/age, and the reason clean combination could not free capacity. Emit a distinct event/counter when the operation exceeds the sealed target, and expose the count and frequency in queue status/metrics. Count committed transitions rather than repeated reconciliation attempts. This is controller bookkeeping; agents provide no extra report. Sealing under this exception does not itself bypass the background-CI launch gate.

## Borrowing and candidate adoption

Use expiring renewable claims for scripted mutations and agent attempts. Borrowing records batch identity/version, starting tip, operation, worker/run identity, and a monotonically changing claim token. Take multiple claims in a consistent order or one transaction. Keep the queue's sealed-prefix invariant when sealing or moving batches.

Workers use private worktrees/refs and return proposed commit chains. Only the controller adopts an authoritative batch tip after validating membership, history, expected batch version, and current claim. Expiry makes work eligible for recovery; it is not proof that the old process stopped. A late result cannot overwrite newer state, but its commits remain retained and can be reused.

Keep an active tool call running through coordinator/network failure. The harness preserves progress, retries communication, and reconciles on reconnect. Repeated loss of bookkeeping must not discard source or repeatedly restart the same model session. Actual publication still requires the necessary source/CI/ownership facts.

## Head operations and CI

The controller repeatedly reconciles the head:

1. If it is unsealed and nonempty, seal it as soon as its current borrow is released. No collection window or additional insertion pass delays a wanted seal. Remove an idle empty placeholder with no reserved insertion instead of sealing it or running empty staging CI.
2. If it is sealed but based on an older canonical commit, borrow it and attempt a scripted rebase onto current `main`. On conflict, restore the previous authoritative series and make that rebase-conflict task available to an agent, with the failed probe's evidence retained.
3. Once it is sealed and based on current `main`, start or observe the required CI programmatically. Wait for a running check; recover infrastructure failures separately from source failures.
4. If source-dependent CI fails, prioritize an agent repair of that batch. Adopt the updated series, then let the controller run CI on it again.
5. If required CI passes, fast-forward `main` from the recorded expected parent to the batch tip. Create no additional batch squash or merge commit. If the expected parent changed, return to rebase/validation. GitHub does not classify a PR as merged when its squash commit lands this way; PRs close by reference, as with Bors today, and landing evidence stays positive through the `Source-PR` trailer and retained refs.

CI scheduling, result classification, retry, and promotion are controller operations. Agents can use local verification while editing, but do not decide whether required CI ran or which checks permit publication. A CI result is bound to the actual candidate and checking configuration; an old callback cannot mark a new source green. Source-dependent checks and cancelled/lost runners or missing status publication take different recovery paths.

A batch leaving the head exposes its successor for the same reconciliation. Keep accepted membership and useful source during admin merges or queue restarts. A canonical advance does not reset original PR review.

## Background sealed batches and runtime controls

Do no CI on unsealed batches. Sealed non-head batches can run CI and receive agent repair before reaching the head, controlled by an easy-to-change runtime flag. Background results apply to the source tested; after a new base is integrated, obtain the results required for that candidate.

Expose at least these runtime controls through the existing administration surface or protected service/repository settings, without a code PR:

| Setting | Meaning |
| --- | --- |
| `max_unsealed_batches` | Maximum number of unsealed batches; initial value 4 |
| `max_sealed_batches` | Normal target/limit on sealed batches, including the head; initial value 2, replenished immediately when work/capacity permit; logged cap-pressure sealing can exceed it |
| `background_sealed_ci_and_repair` | Enable/disable CI and source repair for sealed non-head batches |
| `background_ci_backlog_threshold` | Permit new proactive sealing/background CI when relevant queued CI jobs are below this threshold; initial value 100 |
| `insertion_age_window` | Number of oldest approved PRs the random insertion draw is taken from; initial value 16 |

Apply changes to new scheduling decisions and report effective values. Lowering a cap does not evict existing batches or interrupt useful attempts. Disabling background work stops new background launches while retaining current source/results. Head repair and validation take priority over background work. The backlog gate controls speculative work; it does not prevent necessary head validation or repair.

Background CI/repair operates on independently based batches. It does not require stacking batches on their predecessors. Proactively rebase sealed non-head batches toward current `main` when background work is enabled. Coalesce repeated advances into a pending refresh rather than repeatedly restarting an active attempt. The same background capacity gate should apply when a refresh would launch more CI; retain useful work when the backlog rises.

Measure backlog from jobs queued for execution on the runner pools used by staging, including ordinary PR jobs competing for the same capacity. Exclude running jobs and jobs using unrelated runner pools. Count jobs rather than PRs or workflow runs. Reuse an existing observation feed or cached/batched reads rather than adding per-batch GitHub polling. If the observation is stale or unavailable, use zero backlog for scheduling. Report observation age/availability and whether zero was assumed. Selecting the observation source and freshness interval is an implementation detail, not a prerequisite policy decision.

## Repair agent execution

In the first increment, conflict and repair agents run only on the worker fleet through the existing `TauCetiWorker` harness, using each worker's own Opus API credentials or Claude subscription access. The queue exposes a named borrowed task; a worker claims it, receives the normal source and diagnostics, and returns a proposed attributed chain. Its output is never permission to bypass the subsequent scripted CI.

Authorize worker execution using the existing security-audit rule. At the time of this plan, `tauceti_worker/security_audit.py::require_admin` requires repository-admin permission, rather than named-team membership. Reuse the actual predicate, verifying the selected GitHub account at claim and result adoption. A PR comment or local declaration of membership is not authorization.

The agent must have the failed candidate's compiled artifacts. For a CI repair task the controller records where the exact failed commit's build outputs live (the staging cache the existing finalizer confirms, or the workflow run's artifacts) and the harness restores them before the agent starts, so that each repair iteration is an incremental build of the affected modules rather than a cold build longer than the CI run it is fixing. For a conflict task, the artifacts of the batch's recorded base serve the same purpose. A task whose artifacts cannot be restored is reported as such; it is not silently run cold.

Running agents as GitHub Actions jobs with an API key stored as a repository secret is deferred. If adopted later, that job holds a credential while reading PR-authored text and running tools, so it needs an egress allowlist limited to the model API and GitHub, a spend-capped key, and the stated assumption that PR text is untrusted input. Credentials and trusted workflow code remain outside candidate-controlled builds; ordinary source checks keep their existing isolation.

Every execution path shares the same claims. A delayed or duplicate attempt cannot create a competing authoritative writer.

## Inference unavailability

When no execution path can obtain inference (provider errors or exhausted credentials across every configured path, after bounded retries), the queue holds: it stops sealing new batches, the head waits with its membership and retained source intact, and blocked repair tasks remain exposed to any authorized worker that can still run. The controller retries with backoff and resumes automatically when a task completes or a provider call succeeds. There is no conventional-splitting fallback, no return of members to authors, and no operator page. A difficult source problem or an ordinary infrastructure error is not evidence that inference is unavailable.

Queue status reports the hold, its start time, and the last provider error so the cause is visible after the fact.

## State, existing code, and implementation

Implement the queue as a separate Python service, not as a module inside the Bors deployment. Bors keeps serving repositories that have not opted in until it is retired; for an opted-in repository the new service owns approval intake, batch construction, CI dispatch, promotion, and comments, and Bors is disabled for that repository. The service shares a language with `TauCetiWorker` and `TauCetiReview`, where claims, the repair harness, retained source, and authorization already live, and its adapters are small: approval intake reads the existing labels and review decisions, CI dispatch is the existing `repository_dispatch` of `pr-build.yml` with the batch's immutable ref, results are commit statuses and check runs, promotion is a fast-forward push, and comments are ordinary issue comments with idempotency markers.

Keeping the engine inside Bors would reuse its GitHub client, outbox, and candidate journal, but would also inherit the batcher, divider, train, and isolation code it is meant to replace, in a language none of the surrounding tooling uses.

Records are the service's own, in one database:

- approved input: repository, PR, accepted head, description, author and coauthors, admission evidence, retained object ref;
- batch: identity, version, base commit, tip commit, sealed flag, ordered member list with per-member commit and provenance (clean replay or agent-edited), claim state, CI attempts and results;
- claim: batch identity and version, operation, worker or run identity, token, expiry;
- pending side effects: promotion, comments, label updates, each with an idempotency key and attempt history.

Explanations stay in commit messages. Source mutation, record publication, and GitHub side effects need restart reconciliation because they cannot all share one transaction.

Inspect these effective implementations and their repository instructions before building the adapters:

- `TauCetiProject/bors-ng`: approval intent, CI dispatch and status binding, promotion, comment idempotency, and the observation and candidate endpoints that `TauCetiReview` and the workers read.
- `TauCetiProject/TauCeti/.github/workflows/`: actual admission, staging CI, and publication callers and their pinned shared implementations.
- `TauCetiProject/TauCetiReview`: existing admission/merge-policy evaluation and the representation of `awaiting-author` work.
- `TauCetiProject/TauCetiWorker`: `work_units.py`, `round.py`, `repair_recovery.py`, and `security_audit.py` for harness execution, claims, retained source, and authorization.

First build a local end-to-end fixture using real Git history and scripted stand-ins for AI/provider responses. It should demonstrate clean insertion/rebase without inference, conflict resolution and CI repair producing a replacement series, script-controlled checks, fast-forward publication, and retained membership/explanations. Exercise independent batches, borrowing, age-windowed random insertion probes, runtime flags, shrink-to-green repair, and the inference-unavailable hold.

Then connect the existing admission, harness, CI, and publication paths, first in a shadow mode that constructs batches from the same approvals without publishing, so that its decisions can be compared with Bors before it takes ownership of a repository. Prepare a live activation plan accounting for legacy splitting, author-repair delegation, and publication ownership. Keep normal worker/CI admissions enabled and preserve in-flight useful work. Verify running revisions, actual completions, and interrupted/stranded tasks after rollout. No routine fleet drain is part of deployment.

Expose queue/head status, effective settings, sealed/borrowed state, current source, pending task, CI state, and reasons for waiting, as a JSON endpoint with a stable schema version, in the manner of the existing `tauceti-bors.observation/v1` feed. Measure contributions landed/hour, head rebase/CI/repair times, background useful versus discarded work, claim collisions, PR waiting age, repeated builds/model work, shrink-to-green occurrences, inference holds, and cap-pressure sealing/target-overflow counts and frequency. The head cycle time is CI plus, for each repair round, agent time plus CI; with 25-minute CI, 20-minute repair, and two repair rounds for a 110-PR batch, that is about 115 minutes per batch, so the sustained rate is set by batch size and repair rounds, not by CI duration alone. Record both so the pilot can be compared with this model. Use natural authorized work for a pilot; fixtures use no paid inference.

## Policy status

The policy choices needed for the initial implementation are settled: start with an unsealed cap of 4, a normal sealed target of 2, and a background CI backlog threshold of 100 queued jobs competing for staging runners. Assume zero backlog when observations are stale or unavailable. These capacity values are runtime-configurable without a PR.

The initial target of two sealed batches and the separate Python queue service are decided. Seal wanted batches immediately when available, with no collection delay. Cap pressure can exceed the sealed target through clearly logged sealing operations. Caps, age-windowed random insertion probes, creation below the cap while existing unsealed batches are borrowed, no membership eviction with shrink-to-green as the agent's only exit, no stacking on unlanded predecessors, no unsealed CI, runtime-controlled background sealed work and proactive rebasing, lazy clean combination, message-only explanations, worker-only agent execution in the first increment, and hold-and-retry on inference unavailability are established requirements. Problems discovered after staging admission are addressed through follow-up PRs to Tau Ceti; there is no exceptional-withdrawal process to implement.

### Sealing policy

Initially target two sealed batches: the head in validation/repair and one successor already sealed and available for background CI/repair. Unsealed assembly can continue behind them. When the head lands, promote the successor and replenish the sealed prefix as capacity permits. The successor can still require rebase and CI against the newly advanced canonical branch; prior sealing/background work reduces preparation time but does not establish that final candidate's green status.

When a sealed batch is wanted, seal the first unsealed batch immediately when it is nonempty and available. There is no collection delay, age trigger, or requirement to finish an additional insertion pass. An existing borrow can finish, but new insertion work must not repeatedly postpone a wanted seal. A CI backlog threshold must not stop the queue from starting its necessary head build.

For ordinary advance sealing, require space under `max_sealed_batches`, background work enabled, and a relevant CI backlog below the runtime threshold (initial value 100). Assume zero when the observation is stale or unavailable. Immediately fill the second sealed slot under these conditions when a batch is available. The cap-pressure exception can seal beyond the target; background CI launches remain subject to their capacity gate.

Only the first unsealed batch may be sealed without moving queue positions. If it is borrowed, record that sealing is wanted and avoid repeatedly giving it new insertion work after the borrow returns. This does not cancel or preempt the current attempt. Sealing a later batch while an earlier one remains unsealed would violate the sealed-prefix invariant.

After lazy clean combination fails under cap pressure, seal the first unsealed batch when it is nonempty and available even when the sealed target is reached, recording the operation as specified above. If it is borrowed, mark it for sealing on release and recover abandoned claims normally; do not exceed the unsealed cap while waiting or seal a later batch out of queue order.

## Acceptance scenarios

1. Clean insertion, batch rebase, CI launch/observation, and publication complete with zero agent invocations.
2. A conflict or source-dependent CI failure produces a narrowly identified AI task and a validated replacement chain; there is no staging review.
3. Every final commit is single-parent and maps bijectively to a member PR, including empty contributions. Validation accepts a membership-equivalent replacement chain without comparing member positions.
4. Agent edits preserve each previous message as a prefix and append concise reasons. Comments are generated from those messages; no extra agent decision file/report is required.
5. A mechanical rebase does not create spurious content-modification comments, while agent-modified contributions get their final linked comment with independently retryable publication.
6. PR reservation, borrowing, claim expiry, late results, and two-batch combination cannot duplicate or lose members or overwrite a newer candidate.
7. The sealed-prefix invariant survives insertion, sealing, combination, promotion, and restart. Borrowed batches are protected from concurrent mutation. A wanted seal has no artificial collection delay. Cap pressure can exceed the sealed target, with one clearly logged/counted event per adopted overflow transition.
8. Unsealed batches never launch CI. Initial runtime values are 4 unsealed batches, a normal target of 2 sealed batches, and a threshold of 100 queued jobs sharing staging capacity. Settings can change without a PR. Stale/unavailable backlog observations use zero and do not block background scheduling. Existing borrowed batches allow creation below the cap; no normal eviction occurs. Observed backlog pressure gates speculative work while necessary head work continues. Proactive rebasing coalesces canonical advances and preserves active work.
9. Lazy clean combination is attempted under cap pressure, retains provenance, and leaves conflicting batches intact without an AI-combination task.
10. Admin advances and lost promotion responses reconcile correctly; promotion adds no merge commit, and stale CI cannot approve changed source.
11. An authorized worker claims a repair task, restores the failed commit's compiled artifacts before the agent starts, and returns a chain the controller validates; a second claimant or a late result cannot become a competing writer.
12. Coordinator outages preserve active tool calls and useful source; restart recovers actual worker/CI/publication state before repeating work.
13. When every inference path is unavailable, the queue holds with all membership and retained source intact, reports the hold and its cause in status, and resumes automatically on the first successful task or provider call. No member changes state during the hold.
14. A contribution the agent cannot make pass is shrunk, down to an empty commit, and the batch still lands; its PR comment names what was dropped. No repair task is ever abandoned for lack of a time budget.
15. Live activation/rollback retains useful source and declared ownership of every pending batch/member.
16. A problem reported by an author or reviewer after staging admission does not withdraw the contribution or reopen admission review. The batch lands the AI's best-effort integration after required CI; corrections are separate follow-up PRs to Tau Ceti.
17. A sealed batch of 110 PRs containing two source failures lands with zero singleton CI runs and at most three full CI runs, and its head time from first dispatch to fast-forward is under 120 minutes with warm artifacts available to the repair agent.
18. No approved PR waits more than `insertion_age_window` insertion passes without being drawn.

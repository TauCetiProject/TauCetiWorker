# AI merge queue implementation plan

Implement a scripted merge queue with AI assistance for merge conflicts and source-dependent CI failures. Scripts apply clean changes, construct attributed commit chains, schedule and interpret CI, and publish successful batches. Agents resolve conflicts and fix failing source using their judgment, preserving the intent of the constituent PRs as best they can.

This is a standalone plan for the existing GitHub PR, agent, and CI workflow. All required behavior is described here. Examples describe interfaces to implement.

## Increments

The plan is delivered in three steps. Each is useful on its own and none depends on a later one.

**Increment 0: risk reduction ahead of the queue.** The untested claim in this plan is that an agent, given a failed candidate with complete diagnostics and warm build artifacts, produces a green replacement chain faster than the author round-trip (observed in the [staging triage](staging-triage.md) deployment: 19 to 39 minutes from handoff to a repaired head, then hours behind the backlog). Test that claim before building the controller:

- *Historical repair experiments.* The Bors candidate journal (`/repositories/1/candidates?base=main`, 100 most recent candidates) and the retained `staging-candidate-N-G` refs give every failed candidate of the last day with its exact commit, base, members, diagnostics and attributed owners. Run the repair agent of this plan over those past failures, in parallel, without waiting for new ones: for each failed candidate with compiler, audit or lint diagnostics, check out its commit in a private worktree, prepare artifacts from the published `main` cache at its base plus an incremental build of the batch's modules, run the agent with the instruction below and the real diagnostics, and record time to a green local build of the affected modules, which PRs it edited, whether it shrank a contribution, the diff, and inference cost. Nothing is published. Twenty candidates across at least five distinct failure lineages is the sample; the repair time distribution and the warm-artifact question (whether `main`'s cache plus an incremental build is warm enough, or the failed candidate's own outputs are needed) are decided by this data, and the results table is committed next to the plan. The same harness later runs on live failures as the increment 1 repair task without adoption.
- *Attributed chain construction* as a standalone tool with Git fixtures: build the per-PR squash chain from member heads with the trailers below, validate single-parent history and the member bijection. The shadow repair task uses it to hand the agent a chain, so it is exercised long before the controller exists.
- *Readmitted suspects keep their queue position* in Bors: a suspect handed to repair and re-approved at a new head inherits its original admission age once, instead of re-entering behind the backlog. This is the largest remaining latency win in the current system and a small change to `queue_age.ex` and the handoff record.
- *Journal report* as a committed script producing the triage measurements (slot occupancy by category, failed rounds per landed large batch, singleton and bisection counts, suspects per partition, handoff-to-readmission time), so every change from here on has a before and after.
- *The queue service's GitHub App*, created and installed now; the shadow task and the status endpoint run on its API budget.

**Increment 1: one open batch and one head.** Approved PRs are inserted into a single open batch. When the head lands, the open batch seals and becomes the head, and a new open batch starts. The head is rebased, built, repaired in place and fast-forwarded. There are no successor batches, no background CI, no combination, no cap pressure, no agent-assisted insertion, and no clean-replay comparison for comments. This is the increment the rest of this document specifies unless a section says otherwise. The trade-off is that a repair round is dead time for the whole queue; there is no evidence yet that pipelining shortens the head cycle, and the head cycle is what the pilot measures first.

**Later increments**, each adopted only when the measurements of increment 1 show the need: sealed successor batches with background CI and repair, lazy combination of open batches under a cap, cap-pressure sealing with its logging, agent-assisted insertion of PRs that do not replay cleanly, the clean-replay comparison for comment generation, and agents hosted in GitHub Actions. Their specifications are collected at the end of this document.

## Queue and source model

Maintain a queue of approved PR revisions not yet inserted into a batch, one open batch receiving insertions, and one sealed head being validated. Each batch is a linear chain of single-parent commits based on a recorded canonical commit, containing exactly one squash commit per constituent PR, including an empty commit where appropriate. A batch is never stacked on an unlanded predecessor: a red result on a stacked candidate cannot be assigned to the predecessor or the successor without another build, so it is not a repair task, and concurrent repair of both would invalidate each other's work.

Sealed membership is fixed; the open batch receives approved PRs until it seals. Each batch records its claim state, source base/tip, and CI results. The sealed head can be claimed for repair.

Once a PR enters staging, land the AI's best effort at merging/fixing its content through the scripted CI and publication process. There is no exceptional-withdrawal process. If the original author or a reviewer subsequently finds a problem, they address it in a follow-up PR to Tau Ceti. Such findings do not withdraw the staged contribution or reopen its admission review. The same holds for automation: review, repair and housekeeping pipelines must not close, relabel, re-review or withdraw a PR that is a batch member, and the queue publishes membership (a label and a status on the head) that those pipelines honour. A member PR closed after staging admission still lands; closure is not withdrawal.

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

A green batch takes priority over preserving any one contribution's intent. When an agent cannot make a constituent commit compile and pass CI within the batch, it shrinks that contribution to the part that does, down to an empty commit if necessary, and the appended message and the programmatic PR comment say what was dropped. This is the only exit from a repair task. Membership is never evicted and no repair has a time budget, because the agent can always reach green.

The batch tip is the unit that must pass CI. Intermediate constituent commits need not each pass independently.

## Explanations and PR comments

An agent's explanations belong only in commit messages. For each constituent commit whose content it deliberately changes, it appends a very concise explanation of what changed and why, naming conflicting PRs where useful. Preserve the previous message text. Do not ask for a separate summary file, structured decision report, or second explanation through the queue API.

Agent instruction:

> Resolve the reported conflicts or CI failures in this batch. Preserve the constituent PRs' intent as best you can; where they conflict, use your judgment to choose the best alternative or combination. Update the constituent commits, retaining their source PR identities. When you change a commit's content, append a very concise explanation of what changed and why to its existing message. If a contribution cannot be made to pass, reduce it to the part that does, down to an empty commit, and say so in its message.

The controller records provenance per member commit: clean replay, or agent-edited in a named repair task. A mechanical rebase changing commit hashes does not by itself mean a PR's content was modified; replaying descendants after an earlier edit is not an edit to every descendant. A conflict requiring agent resolution is an edited contribution.

Check append-only message preservation against the corresponding prior commit by PR identity. Collect explanations from the appended message text. If an agent has changed source without the required message addition, have the same attempt add the concise explanation; no new review phase is introduced.

Post a PR comment when the member's provenance is agent-edited. Reuse commit-message explanations directly, with links to the relevant retained commits and conflicting PRs. Comment construction/publication is programmatic. Do not add a mandatory post-hoc AI analysis to reconstruct decisions already made. Comparing the landed content with a clean replay of the accepted branch, to suppress comments on edits that turned out to be no-ops, is a later increment.

Publish one concise consolidated comment per PR for the actual landed result. Failed comment publication is retried independently of source landing. Preserve modified commit objects under durable refs so comments and links remain usable after further rewrites. A separate decision store is unnecessary; operational records can identify inputs, outputs, and modified commits without duplicating the reasons in their messages.

## Scripted insertion

For each insertion pass, draw the next PR at random from the `insertion_age_window` oldest approved PRs. The age window prevents starvation. For the chosen PR, use a private workspace to attempt a clean replay/squash onto the open batch. Claim and recheck the batch's version before committing the insertion. Concurrent attempts must also reserve the approved PR so it cannot be inserted twice.

Commit the new batch tip, membership, and removal from the waiting queue through a recoverable operation. A failed or interrupted probe leaves the approved PR represented and the existing batch intact. PRs need not form a strict global FIFO beyond the age window.

A PR that does not replay cleanly onto the open batch stays approved and waiting, and is probed again on later passes and after the head lands, since the conflicting content is usually a batch-mate that lands with the head. A PR whose head conflicts with `main` itself receives the ordinary conflict request. No agent is launched to insert a PR in this increment.

A clean textual replay is not enough for a PR that removes or renames a module: the deleted source may still be imported by a module that landed on `main` after the PR's head was built, or by a batch-mate, and both heads are individually green. Before committing the insertion, read the import headers of the prospective batch tree (the same whole-tree check the CI preflight runs, with headers cached by blob) and refuse the PR with the ordinary missing-source request when any import has no source. The declaration-name records are checked the same way at the same point. On 2026-11-04 one such deletion (#26731 against a module added by #26762) cost Bors a 123-PR head build plus a bisection; the check costs under a second.

An operator can mark an approved PR urgent. An urgent PR is inserted into the open batch, which is then sealed as soon as the head lands; it never preempts or cancels the running head, and it never requires an administrative merge around the queue. Administrative merges that do happen are reconciled as canonical advances, as specified under head operations; they are an exception path, not the way infrastructure changes land.

If no open batch exists, create one when approved work is waiting. Remove an idle empty open batch rather than sealing it or running empty staging CI.

## Claims and candidate adoption

Use expiring renewable claims for scripted mutations and agent attempts. A claim records batch identity/version, starting tip, operation, worker/run identity, and a monotonically changing claim token. Take multiple claims in a consistent order or one transaction.

Workers use private worktrees/refs and return proposed commit chains. Only the controller adopts an authoritative batch tip after validating membership, history, expected batch version, and current claim. Expiry makes work eligible for recovery; it is not proof that the old process stopped. A late result cannot overwrite newer state, but its commits remain retained and can be reused.

Keep an active tool call running through coordinator/network failure. The harness preserves progress, retries communication, and reconciles on reconnect. Repeated loss of bookkeeping must not discard source or repeatedly restart the same model session. Actual publication still requires the necessary source/CI/ownership facts.

## Head operations and CI

The controller repeatedly reconciles the head:

1. When no head exists and the open batch is nonempty, seal the open batch as soon as its current claim is released and start a new open batch. No collection window or additional insertion pass delays the seal.
2. If the head is based on an older canonical commit, claim it and attempt a scripted rebase onto current `main`. On conflict, restore the previous authoritative series and make that rebase-conflict task available to an agent, with the failed probe's evidence retained.
3. Once it is based on current `main`, start or observe the required CI programmatically. Wait for a running check; recover infrastructure failures separately from source failures.
4. If source-dependent CI fails, make the repair task available to an agent. Adopt the updated series, then let the controller run CI on it again.
5. If required CI passes, fast-forward `main` from the recorded expected parent to the batch tip. Create no additional batch squash or merge commit. If the expected parent changed, return to rebase/validation. GitHub does not classify a PR as merged when its squash commit lands this way; PRs close by reference, as with Bors today, and landing evidence stays positive through the `Source-PR` trailer and retained refs.

CI scheduling, result classification, retry, and promotion are controller operations. Agents can use local verification while editing, but do not decide whether required CI ran or which checks permit publication. A CI result is bound to the actual candidate, run and checking configuration; an old callback cannot mark a new source green, and a cancelled or superseded run cannot mark a candidate red. The controller owns the dispatch and the concurrency of candidate builds: a pull-request event, a push to a member branch, or a workflow's always-run reporting step must not cancel a candidate build or post a verdict in its place, and no status that did not come from the candidate's own completed run is read as its verdict. An approval is never consumed by a stale or cancelled status. Source-dependent checks and cancelled/lost runners or missing status publication take different recovery paths.

Keep accepted membership and useful source during admin merges or queue restarts. A canonical advance does not reset original PR review.

Routine rechecks of member PRs are not withdrawals. Label sweeps, eligibility reconciles and status polls run continuously, about one per minute across the open PR population, and a head of a hundred PRs always has one in flight while it is being sealed, rebased or dispatched. Only an actual withdrawal signal (a cancel command, a changed head, a closed or retargeted PR) may make a claimed preparation yield; a pending recheck neither delays nor restarts it, and a recheck that later finds the PR ineligible removes it through the ordinary cancel path. Bors yielded to every pending recheck and restarted the construction of each large batch four or five times, five to eight minutes per batch.

## Staging capacity and build progress

The head build is the serial resource. Staging builds keep running on the fleet hosts as they do today; the controller records which runner and host executed each candidate build and how long each build phase took, so placement decisions can be made from measurements.

Status distinguishes a slow build from a stalled one: the build publishes phase progress (preparation, cache restore, build with module counts, audits, lint) with timestamps, and the controller raises a visible stall condition when a phase makes no progress for `stall_interval_seconds`. Operators must be able to answer "which machine is this running on and is it moving" from queue status alone, without access to the runner host or to in-progress workflow logs.

The CI contract for a candidate is the one established by the staging triage work: the build runs to completion and reports every independently failing module, and audit and lint findings carry the defining module path. The repair agent's inputs depend on that contract.

## Runtime controls

Expose these runtime controls through the existing administration surface or protected service settings, without a code PR. Apply changes to new scheduling decisions and report effective values.

| Setting | Meaning |
| --- | --- |
| `insertion_age_window` | Number of oldest approved PRs the random insertion draw is taken from; initial value 16 |
| `repair_concurrency` | Maximum concurrent agent sessions on the head's repair task; initial value 1 |
| `stall_interval_seconds` | Build phase silence after which status raises a stall condition; initial value 900 |

## Repair agent execution

Conflict and repair agents run on the worker fleet through the existing `TauCetiWorker` harness, using each worker's own Opus API credentials or Claude subscription access. The queue exposes a named claimed task; a worker claims it, receives the normal source and diagnostics, and returns a proposed attributed chain. Its output is never permission to bypass the subsequent scripted CI. The shadow repair task of increment 0 is this task without adoption or publication.

Authorize worker execution using the existing security-audit rule. At the time of this plan, `tauceti_worker/security_audit.py::require_admin` requires repository-admin permission, rather than named-team membership. Reuse the actual predicate, verifying the selected GitHub account at claim and result adoption. A PR comment or local declaration of membership is not authorization.

The agent must have warm build artifacts. The default source is the published `main` cache plus an incremental build of the batch's own modules, which is what workers already do for PR repair; the shadow repair measurements decide whether the failed candidate's own outputs (the staging cache the existing finalizer confirms, or the workflow run's artifacts) must be restored instead. For a conflict task, the artifacts of the batch's recorded base serve the same purpose. A task whose artifacts cannot be prepared is reported as such; it is not silently run cold.

Every execution path shares the same claims. A delayed or duplicate attempt cannot create a competing authoritative writer.

## Inference unavailability

When no execution path can obtain inference (provider errors or exhausted credentials across every configured path, after bounded retries), the queue holds: it stops sealing, the head waits with its membership and retained source intact, and blocked repair tasks remain exposed to any authorized worker that can still run. The controller retries with backoff and resumes automatically when a task completes or a provider call succeeds. There is no conventional-splitting fallback, no return of members to authors, and no operator page. A difficult source problem or an ordinary infrastructure error is not evidence that inference is unavailable.

Queue status reports the hold, its start time, and the last provider error so the cause is visible after the fact.

GitHub authorization failures hold the queue the same way. When every GitHub call is rejected with a permanent authorization failure (a suspended installation, a revoked or expired token, a lost permission), the controller enters the hold within one reconcile instead of retrying each call: no claim may expire because of retries against a rejecting API, no job may be requeued and reclaimed ahead of the work it blocks, and status names the failure. Work resumes on the first successful call. When Bors's installation was suspended on 2026-11-04 its reads were retried for three minutes each, the job overran its lease, was requeued and reclaimed first, and the queue stopped with thousands of jobs waiting and nothing in status saying why.

## State, existing code, and implementation

Implement the queue as a separate Python service, not as a module inside the Bors deployment. The service acts through its own GitHub App installation, with its own API budget; it never shares a personal account's rate limit with the worker fleet, which exhausts that limit several times a day. It is event-driven (webhooks for statuses, check runs, pushes and comments) with bounded, batched polling only as a fallback. Bors keeps serving repositories that have not opted in until it is retired; for an opted-in repository the new service owns approval intake, batch construction, CI dispatch, promotion, and comments, and Bors is disabled for that repository. The service shares a language with `TauCetiWorker` and `TauCetiReview`, where claims, the repair harness, retained source, and authorization already live, and its adapters are small: approval intake reads the existing labels and review decisions, CI dispatch is the existing `repository_dispatch` of `pr-build.yml` with the batch's immutable ref, results are commit statuses and check runs, promotion is a fast-forward push, and comments are ordinary issue comments with idempotency markers.

Keeping the engine inside Bors would reuse its GitHub client, outbox, and candidate journal, but would also inherit the batcher, divider, train, and isolation code it is meant to replace, in a language none of the surrounding tooling uses.

Records are the service's own, in one database:

- approved input: repository, PR, accepted head, description, author and coauthors, admission evidence, retained object ref;
- batch: identity, version, base commit, tip commit, sealed flag, ordered member list with per-member commit and provenance (clean replay or agent-edited), claim state, CI attempts and results with runner placement and phase timings;
- claim: batch identity and version, operation, worker or run identity, token, expiry;
- pending side effects: promotion, comments, label updates, each with an idempotency key and attempt history.

Explanations stay in commit messages. Source mutation, record publication, and GitHub side effects need restart reconciliation because they cannot all share one transaction.

Inspect these effective implementations and their repository instructions before building the adapters:

- `TauCetiProject/bors-ng`: approval intent, CI dispatch and status binding, promotion, comment idempotency, and the observation and candidate endpoints that `TauCetiReview` and the workers read.
- `TauCetiProject/TauCeti/.github/workflows/`: actual admission, staging CI, and publication callers and their pinned shared implementations.
- `TauCetiProject/TauCetiReview`: existing admission/merge-policy evaluation and the representation of `awaiting-author` work.
- `TauCetiProject/TauCetiWorker`: `work_units.py`, `round.py`, `repair_recovery.py`, and `security_audit.py` for harness execution, claims, retained source, and authorization.

First build a local end-to-end fixture using real Git history and scripted stand-ins for AI/provider responses. It should demonstrate clean insertion/rebase without inference, conflict resolution and CI repair producing a replacement series, script-controlled checks, fast-forward publication, and retained membership/explanations. Exercise sealing, claims, age-windowed random insertion, the urgent lane, shrink-to-green repair, and the inference-unavailable hold.

Then connect the existing admission, harness, CI, and publication paths, first in a shadow mode that constructs batches from the same approvals without publishing, so that its decisions can be compared with Bors before it takes ownership of a repository. Prepare a live activation plan accounting for legacy splitting, author-repair delegation, and publication ownership. Keep normal worker/CI admissions enabled and preserve in-flight useful work. Verify running revisions, actual completions, and interrupted/stranded tasks after rollout. No routine fleet drain is part of deployment.

Expose queue/head status, effective settings, claim state, current source, pending task, CI state (including runner placement and phase progress), and reasons for waiting, as a JSON endpoint with a stable schema version, in the manner of the existing `tauceti-bors.observation/v1` feed. The same endpoint reports the service's running revision (git SHA) and start time, so a deployment can be verified directly rather than inferred from the next natural event. Measure contributions landed/hour, head rebase/CI/repair times, claim collisions, PR waiting age, repeated builds/model work, shrink-to-green occurrences, and inference holds. The head cycle time is CI plus, for each repair round, agent time plus CI; with 25-minute CI, 20-minute repair, and two repair rounds for a 110-PR batch, that is about 115 minutes per batch, so the sustained rate is set by batch size and repair rounds, not by CI duration alone. Record both so the pilot can be compared with this model. Use natural authorized work for a pilot; fixtures use no paid inference.

## Policy status

The policy choices for increment 1 are settled: one open batch and one sealed head, sealing immediately when the head lands, no stacking on unlanded predecessors, no membership eviction with shrink-to-green as the agent's only exit, age-windowed random insertion with no agent-assisted insertion, provenance-flagged comments, worker-only agent execution with warm artifacts, hold-and-retry on inference unavailability, verdicts bound to a candidate's own completed run only, membership that other automation cannot withdraw, an operator urgent lane that never preempts the head, recorded build placement and phase progress, the controller's own GitHub App identity with a reported running revision, and the separate Python queue service. Problems discovered after staging admission are addressed through follow-up PRs to Tau Ceti; there is no exceptional-withdrawal process to implement.

The increment 0 items precede the controller: historical repair experiments, the attributed chain tool, readmission age in Bors, the journal report, and the GitHub App.

The service is a drop-in replacement for Bors's batch policy, not a new interface: it accepts the same approval commands (`bors merge`, `bors r+`, `p=`, `bors r-`, `bors cancel`) from the same actors, publishes the same `tauceti-bors.observation/v1` and `tauceti-bors.candidates/v1` feeds at the same paths, and keeps the same labels, statuses and PR comments, so TauCetiReview, the scope-approval check, the workers' activity accounting and the fleet continue unchanged across the cutover and back.

## Acceptance scenarios

1. Clean insertion, batch rebase, CI launch/observation, and publication complete with zero agent invocations.
2. A conflict or source-dependent CI failure produces a narrowly identified AI task and a validated replacement chain; there is no staging review.
3. Every final commit is single-parent and maps bijectively to a member PR, including empty contributions. Validation accepts a membership-equivalent replacement chain without comparing member positions.
4. Agent edits preserve each previous message as a prefix and append concise reasons. Comments are generated from those messages; no extra agent decision file/report is required.
5. A mechanical rebase does not create a content-modification comment, while agent-edited contributions get their final linked comment with independently retryable publication.
6. PR reservation, claims, claim expiry and late results cannot duplicate or lose members or overwrite a newer candidate.
7. Sealing, promotion and restart preserve exactly one open batch and at most one head; a claimed batch is protected from concurrent mutation; a seal has no artificial collection delay.
8. The open batch never launches CI. Settings can change without a PR.
9. A PR that does not replay cleanly onto the open batch is probed again after the head lands and inserts without an agent once its batch-mate has landed.
10. Admin advances and lost promotion responses reconcile correctly; promotion adds no merge commit, and stale CI cannot approve changed source.
11. An authorized worker claims a repair task, prepares warm artifacts before the agent starts, and returns a chain the controller validates; a second claimant or a late result cannot become a competing writer.
12. Coordinator outages preserve active tool calls and useful source; restart recovers actual worker/CI/publication state before repeating work.
13. When every inference path is unavailable, the queue holds with all membership and retained source intact, reports the hold and its cause in status, and resumes automatically on the first successful task or provider call. No member changes state during the hold.
14. A contribution the agent cannot make pass is shrunk, down to an empty commit, and the batch still lands; its PR comment names what was dropped. No repair task is ever abandoned for lack of a time budget.
15. Live activation retains useful source and declared ownership of every pending batch/member.
16. A problem reported by an author or reviewer after staging admission does not withdraw the contribution or reopen admission review. The batch lands the AI's best-effort integration after required CI; corrections are separate follow-up PRs to Tau Ceti.
17. A sealed batch of 110 PRs containing two source failures lands with zero singleton CI runs and at most three full CI runs, and its head time from first dispatch to fast-forward is under 120 minutes with warm artifacts available to the repair agent.
18. No approved PR waits more than `insertion_age_window` insertion passes without being drawn.
19. A pull-request event or branch push during a candidate build neither cancels that build nor posts a verdict for it; a cancelled or superseded run leaves the candidate's state unchanged and its approval intact.
20. An urgent PR is in the next head and lands without an administrative merge and without cancelling the running head.
21. A review or repair pipeline closing or relabelling a member PR does not change batch membership; the PR's content still lands.
22. Queue status names the runner and host of the running head build, shows phase progress with timestamps, and raises a stall condition when progress stops; it also reports the service's running revision.
23. Increment 0: the historical repair experiments, run over at least twenty past failed candidates, report time to a green local build, edited PRs, shrink events and cost for each, without publishing anything; a suspect readmitted after repair is ordered by its original admission age.
24. Transition: the shadow period shows two days of membership-for-membership agreement with Bors; the rehearsal passes on the scratch repository; cutover is one setting change that preserves every approved input with its original age.
25. A PR that removes or renames a module still imported by an unchanged module on `main` or by a batch-mate is refused at insertion from the import headers, with the ordinary missing-source request, and never reaches head CI.
26. Routine rechecks of member PRs (label sweeps, eligibility reconciles, status polls) never delay or restart a seal, rebase or CI launch; only an actual withdrawal signal does, and a head of 120 PRs is prepared in one attempt while such sweeps run.
27. When GitHub rejects every call with a permanent authorization failure (suspended installation, revoked token), the controller enters a reported hold within one reconcile; no claim expires because of retries, status names the cause, and work resumes on the first successful call.

## Work plan for autonomous implementation

This section is the contract for an agent working toward `/goal implement ai-merge-queue.md`. The work splits into streams that do not share files or state; run them concurrently through subagents, each on its own branch and worktree, and keep the orchestrating session for integration, review and the operator items. Every stream opens its own PR when it compiles and tests pass; do not batch streams into one PR.

The service lives in `TauCetiProject/TauCetiWorker` as the package `tauceti_queue` with its own entrypoint, tests under `tests/`, and documentation under `docs/queue/`, so it shares the harness, claims, security audit and deployment tooling. A separate repository is not needed for increment 1.

| Stream | Needs from other streams | Deliverable |
| --- | --- | --- |
| A. Attributed chain tool | none | `tauceti_queue/chain.py`: build, replay and validate per-PR squash chains from member heads; Git fixtures covering empty commits, rebases, membership-equivalent replacement chains |
| B. Historical repair experiments | A for chain handling; a fleet host with the Mathlib cache and Claude access | the experiment harness, its results table committed to `docs/queue/repair-experiments.md`, and the warm-artifact decision |
| C. Journal report | none | `scripts/staging_journal_report.py` with a fixture and tests; the baseline numbers from the triage window committed |
| D. Readmission age in Bors | none (bors-ng repository) | the `queue_age.ex` and handoff change, admin-merged when green |
| E. Controller and state machine | A | `tauceti_queue/controller.py` with the records above, claims, sealing, head reconciliation, inference hold, urgent lane; the local end-to-end fixture with a fake GitHub and scripted agent stand-ins |
| F. GitHub adapters and compatibility surface | the GitHub App (operator) and a scratch repository | App authentication, webhook receiver, command parsing compatible with Bors, status and check reads, `repository_dispatch` of `pr-build.yml`, fast-forward promotion, comments with idempotency markers, and the two observation feeds byte-compatible with Bors's schemas (validated against saved production responses) |
| G. Repair task runner | A, B's harness | the worker-side task: claim, prepare artifacts, run the agent, return a chain; reuses `work_units.py`, `repair_recovery.py` and `security_audit.py` |
| H. Deployment | the host, tunnel and secrets (operator) | systemd unit, Postgres on the host, configuration, the status endpoint with running revision, the runbook in `docs/queue/operations.md` |
| I. Cutover rehearsal | E, F, G, H | the full transition below executed against the scratch repository with synthetic PRs before any production step |

Streams A, B, C, D and E start immediately. F and G can be developed against fakes and the saved production feed responses before the App exists, then verified live. H and I follow.

Return control to the operator, with the exact request and nothing else pending on it, for these items only; everything else proceeds without asking:

1. Create and install the GitHub App for the service on `TauCetiProject/TauCeti` and the scratch repository: repository permissions contents write, pull requests write, issues write, checks read, statuses write, actions write, workflows write, metadata read; subscribed events pull request, pull request review, issue comment, status, check run, check suite, push, workflow run. Place the App ID and private key where `docs/queue/operations.md` says.
2. Create the scratch repository `TauCetiProject/TauCetiQueueScratch` with the TauCeti workflow files and a small Lean library, for stream I.
3. Provide the host for the service (a fleet host with Postgres and a public HTTPS endpoint for webhooks, through the existing Cloudflare account).
4. Approve the production cutover step below when it is reached; it is a single setting change.

Progress is reported in the PRs and in a running `docs/queue/status.md`: what is merged, what is blocked on which operator item, and the latest experiment numbers. Admin-merge the service's PRs when they are green; this work has the operator's highest priority.

## Transition to production

The transition is live; normal worker and CI admissions stay enabled throughout and nothing is drained.

1. **Shadow.** The service runs beside Bors on production webhooks with publication disabled. It parses the same commands, records the same approvals, builds its open batch and a head from them locally, and reports in status what it would have sealed, dispatched and landed, next to what Bors did. The repair task runs on Bors's live failures without adoption. Exit criterion: two days in which every Bors landing is a landing the service would also have produced, membership for membership, with no crash, no missed command and no divergent approval record.
2. **Rehearsal.** The full cutover is executed on the scratch repository with synthetic PRs, including a conflict, a source failure repaired in place, a shrink-to-green, an urgent PR, a stale-status race and an inference hold. Exit criterion: every acceptance scenario that the scratch repository can express passes there.
3. **Cutover.** One setting change: the merge backend for `TauCetiProject/TauCeti` is switched from Bors to the service (the existing `MERGE_BACKEND` repository variable that the workflow callers already honour, plus the service's own `publication_enabled` flag). At that moment the service imports Bors's current state from the observation feed: every approved PR waiting or in an unsealed batch becomes an approved input with its original admission age, and Bors's running head is left to finish and land under Bors. The service seals its first head only after that landing, which it observes as a canonical advance. Bors receives no new approvals from then on; its remaining sealed batches are released back to approved inputs of the service with their ages. Bors is then retired.
4. **First week.** The first three heads are watched by a person as well as by status: membership, chain validation, dispatch identity, verdict binding, promotion, comments. The journal report runs daily against the service's feed and against the Bors baseline window.

## Production monitoring

Status (the JSON endpoint) is the primary surface; it shows the head's batch, base, tip, claim, CI run, runner and host, phase progress with timestamps, the open batch's size and oldest member age, the number of approved inputs waiting, any stall or inference hold with its start time and cause, the running revision, and the effective settings.

The journal report, run daily and after every change, compares against the triage baseline: landed PRs per hour, head cycle time and its CI/repair split, repair rounds per landed head, repair time per task and inference cost, shrink events and the PRs they touched, singleton builds (expected zero), claim collisions, PR waiting age distribution, inference holds, and stall conditions. Each is reported with the baseline value beside it.

Conditions that stop the pipeline and page nothing but become the first line of status, to be acted on by whoever next looks: a stall condition on the head for longer than two stall intervals, an inference hold older than an hour, a chain that fails validation twice for the same head, a promotion that fails for a reason other than a canonical advance, and any divergence between the service's observation feed and its own records.

The first exit criterion for keeping the service is the plan's throughput scenario: a 110-PR head with two source failures landing in under 120 minutes. The second is a week in which landed PRs per hour exceed the triage-period baseline with no regression in the comment or attribution checks. Failing either after two weeks of pilot triggers a revision of this plan from the measurements, not a longer pilot.

## Follow-ups from the cutover

**Admission-time knowledge Bors used, now unused.** Bors prepared candidates with two checks that kept PRs out of the same batch when a failure was predictable: the declaration-collision check at assembly, which compares the `DECLARATION-NAMES` records each PR's build writes (the constant names the PR adds, read from compiled module headers; [declaration-collisions.md](declaration-collisions.md)) and defers a PR whose added names collide with an accepted batch-mate's, and the merge-selection planning pass, which replays each unit against the base plus the already accepted units and leaves a textually conflicting unit for a later candidate, grouping suspects by shared paths and import components. The queue inserts without either: a textual conflict at insertion is a probe failure that waits for the tip to move, and a duplicate declaration is found by the audit step of the head's CI round. Neither should keep PRs out of a batch any more, but both are knowledge the queue has before CI and currently throws away. Follow-up (operator request, 2026-11-04): give this information to the agents instead. Concretely, store each accepted input's declaration-names record (fetched the way Bors did, from the PR build's log) with the input; at insertion, intersect the added names against the open batch's members and attach a predicted collision as a diagnostic on the member, not a refusal; when the head is dispatched, or better before it, create a repair task for the predicted collisions so the agent resolves them while CI runs the first round rather than after it; do the same for the path and import-component overlaps the conflict planning computed. Measure in the journal report how many collisions were predicted and fixed before a verdict against how many CI still discovered, which decides whether the pre-CI repair round is worth its inference cost. A third admission-time check may exist; the operator did not recall it, so inventory `lib/worker/batcher/` in bors-ng before implementing.

**Five improvements from the first production repair (operator decision, 2026-11-05, all to implement now).** The first repair of a 1,197-member head took an hour of plumbing before the agent started and then spent most of its tool calls on discovery rather than mathematics. In priority order:

1. *Ownership and declaration context up front.* The worker precomputes, per member, the files it changes and the declaration names it adds, removes or renames (text-derived from its diff, marked approximate until the PR build's `DECLARATION-NAMES` record is wired in), and lists the resulting collisions (one name added by two members) and rename ripples (a name removed by one member and still used by another, or by `main`) in the prompt and in `.tauceti-repair/files.jsonl` and `decls.jsonl`. The agent spent dozens of calls deriving exactly this.
2. *Repair while the build streams.* See the next paragraph: fix a module as soon as its error is reported, one `lake build` active at a time.
3. *Warm artifacts for the rebase target.* The worker reported no cache mapping for the target commit although the cache serves it, so the build started from stale outputs; find and fix the mapping lookup so a repair begins from the newest cached `main`.
4. *Parallel agents within one task.* Independent failing modules (different members, disjoint files) go to concurrent agents, each in its own worktree of the chain tip producing fixup commits for its members; the worker folds the fixups into the chain and runs the one verification build. Builds are serialized by a lock; `repair_concurrency` keeps bounding tasks across heads.
5. *Scoped builds for the agent.* A `.tauceti-repair/build <Module> ...` helper builds only the named modules (and their dependents on request), serialized by the same lock, so the agent checks a fix in seconds instead of rerunning the whole library; the final whole-library pass remains the only verdict.

6. *Publish the repair's build outputs for workers.* After a repair's final green `lake build`, the worker publishes the worktree's outputs under the chain tip's revision to a worker-only cache namespace (`tauceti-repair`), so the next repair round on the same head and the next head's agent start warm. CI's verdict is unchanged by this tier.
7. *Sandbox the repair worker's builds.* Every `lake build` the repair runs, the agent's scoped builds through `.tauceti-repair/build` and the worker's final verification pass, runs under the same `bwrap` offline sandbox CI's `sandboxed-build` uses (TauCetiTools `sandbox-build.sh`), regardless of what is later done with the outputs.

*Decided (operator, 2026-11-05 06:40 UTC):* CI consumes the repair's published outputs. The staging CI run fetches the repair cache's outputs for the exact staging tip before `lake build --iofail`, so Lake recompiles only modules whose traces differ, and the audits and lints run on the result as today; the worker's builds are sandboxed (item 7) and the daily from-scratch build (`nightly-verify.yml`, 15:25 UTC, every TauCeti module recompiled, manifest published) is the independent check, with `ci.yml`'s publication on each `main` push as the warm cache for everyone else. A `lean4checker` replay in CI over the published oleans remains a possible later tightening.

13. *The audits run on the worker, not at the agent's discretion.* Head 2's first CI round after a green worker build failed on one duplicate declaration that the agent's own text scan had listed and dismissed as a false positive. The principle: a problem a program can detect is detected by the program and never left to the agent's judgement. The worker runs the same `audit-driver` phases CI runs (import audit, duplicate declarations) and the environment lint on the changed modules after every green build it performs, in the final verification and in the streaming background build, with the TauCetiTools pin the repository names. Findings reach the agent as ordinary diagnostics in `errors.jsonl`, the same shape as compiler errors, and the worker refuses to submit while any remain; the agent only edits. The prompt states that fact in one sentence and asks for no judgement about what is authoritative. The scripted scans (`decls.jsonl`, collisions) stay as navigation aids, not as checks.

14. *Published outputs are keyed by the verified tip, and the controller may move it.* The worker publishes the build outputs of the tip it verified. If `main` advanced during the repair, the controller replays the head onto the new tip before CI (`_rebase_head`), the commit SHAs change, and the published outputs no longer match. The replay is cheap and needs no round, but the CI build then starts from the base cache. Either the worker rebases onto the current tip immediately before verification and publishes that, or the mapping is keyed by tree hash so a replay that leaves the trees unchanged still hits. Not yet scheduled.

15. *Published outputs are fetched by candidate revision, not by exact commit.* Lake reuses an artifact when its content trace matches, never because a commit matches, so CI may safely fetch the outputs published for any nearby tree: the exact head, the batch's previous tips (before a replay onto a moved `main`), the batch's base, and `main`'s tip. The controller sends that list with every CI dispatch (`repair_outputs_revs`), the fetch script takes several revisions and downloads each artifact once, and a missing mapping is skipped. This is what makes item 14's replay harmless. (Decided 2026-11-05, after the operator asked why CI kept rebuilding what the worker had already built.)

16. *The pre-rebased chain is prepared too.* A clean pre-rebase (item 9) produces a tip nobody has built, so a batch that seals without a repair reaches CI with no published outputs. After a pre-rebase is adopted, the controller creates a `prepare` task for the pre-rebased chain on the head's tip: the worker builds it warm from the head tip's published outputs, repairs failures in place, runs the audits, and publishes outputs for the resulting tip; sealing uses that chain, so CI's exact-head fetch hits. The task is superseded when the head's tip changes, and sealing never waits for it.

17. *The base cache lookup must not walk the chain.* CI finds the public cache's outputs by walking first-parent ancestors of the candidate, at most 1,000 of them. A head of 1,197 squash commits puts `main` further back than that, so every CI run of a large batch discarded the base cache and built the whole library from source; the three-hour rounds of head 2 were this, not the size of the change. The fetch for the base must name the base revision the queue dispatched (`LAKE_CACHE_BASE_REV`), and the repair-output fetch must run even when the base lookup found nothing. (Found 2026-11-05 from the attempt 12 log, after the repair-output step completed in zero seconds.)

**Repair while the build still streams.** The first production repair of a 1,197-member head spent its first `lake build --iofail` pass collecting errors (duplicate declarations, unknown identifiers, a deprecation) before touching anything, because the recipe is "let the build finish for the complete list, then fix". Wall-clock is the resource that matters, so the follow-up (operator request, 2026-11-05) is to let the agent start repairing a module as soon as its error is reported, while the same build keeps going on the rest of the library, with exactly one `lake build` active at a time. The constraints to design around: an edited module must not be rebuilt by the running build (it is already failed, so Lake will not revisit it in that pass, but its dependents will fail on the old error and must be rebuilt after the fix); the agent's own verification build of the fixed module waits for the streaming build to finish or runs through Lake's job queue rather than beside it; and the final whole-library pass is still the only verdict. A first version is the worker tailing the build log, handing each new compiler error to the agent as it appears (or spawning one agent per failing module when the failures are independent), and queuing the fixed modules for the next pass. Measure the saving against the sequential recipe before making it the default.

## Pipeline acceleration behind the head (operator decision, 2026-11-05, implement now)

While head 2 was repaired, open batch 3 (207 members) and open batch 4 (8) sat assembled and idle, and the inflow was held down by the agents' backpressure against the queue size, so steady-state heads will be larger. Two steps, in this order:

8. *Background build and repair of open batches on their own base.* Each open batch is built as assembled, on its own base (never stacked on the unlanded head), on the worker host rather than through the staging dispatch, and its internal failures (duplicates among its members, rename ripples among them, members against the base) become repair tasks of a new kind, `prepare`, committed into the members and carried through the later rebase. One agent per batch, bounded by `repair_concurrency`; the head's tasks take priority. A green background build is not a verdict; the head's CI round remains the only one.
9. *Speculative pre-rebase of the next batch, triggered by head state.* Only once the head has a submitted, validated chain awaiting CI (its tip then changes only if CI fails and another repair round edits it) is the next open batch replayed onto the head's tip in the background; conflicts become a repair task on that batch; the replay is redone when the head's tip changes and discarded if the head is reshaped. Alongside, the per-member declaration context is compared across the two batches (names removed or renamed by the head that the next batch uses, names added by both) and handed to that batch's agent as predicted ripples, so the interaction between consecutive batches is repaired before the next head's CI round instead of discovered by it. Rebasing the next batch onto the head before the head's conflicts and build are settled is explicitly not done: a build of the stacked pair would fail on the head's unfinished problems outside the second agent's remit.

## Repair or reject: the author's choice (operator decision, 2026-11-05, implement now)

Repair in place stays the default: the first production head showed that nearly every edit was an interaction between PRs (a batch-mate's rename, a duplicate declaration, a clash with something landed hours earlier) that an author cannot reproduce on their own branch until the other side lands, so rejection would cost each such PR a full head cycle plus a rebase and replace one repair agent with many author agents. Three additions, from the Zulip discussion with a contributor who prefers rejection:

10. *Tell the author what changed.* When a head lands and the queue modified a member, the landing comment on that PR carries the appended explanation and the diff of the landed commit against the accepted head, so a campaigning agent sees the edit without reading history. Every landed commit remains one PR with its `Source-PR:` and `Accepted-Head:` trailers and its original message as a prefix.
11. *An opt-out label.* A PR carrying `dont-repair-in-queue` is never edited by an agent. When its member would need an edit, the agent instead reduces it to an empty commit and the queue withdraws the PR from the head with a `problem-anticipated` state: a comment naming the interacting PR, file and finding (conflict, duplicate declaration, rename ripple, build error), and the fact that the author cannot yet reproduce it because the other side is still in staging. The member leaves the queue; its approval is not reused.
12. *`problem-anticipated` becomes `needs-rebase`.* When the interacting PR lands on `main`, the queue converts the state: it comments again that the problem is now reproducible, applies `needs-rebase`, and the PR re-enters through ordinary re-approval. If the interacting PR itself leaves the queue without landing, the withdrawn PR is re-admitted at its original age with a comment.

Whether the label stays per PR, becomes per author, or is dropped in favour of one policy is decided from use: count opt-outs, repairs and withdrawals in the journal report.

## Monitoring site (sketch, not scheduled)

A web view of the queue for someone arriving cold: a contributor asking "where is my PR", an operator asking "why has nothing landed for an hour", and later a reader asking "what did the agents do last week and what did it cost". Before building it, decide what each of those readers needs to know in the first ten seconds and whether a picture beats a table for it; the sketch below lists candidate content, not a design.

- *The queue as a picture.* One row per batch, oldest at the top: the sealed head with its CI attempt and phase progress, the open batches with their sizes, the waiting inputs as a bar. A member is a small cell in its batch, coloured by state (clean replay, agent-edited, emptied because already on `main`, removed by shrink), so a 1,200-member head reads as a strip rather than a list. Click a cell for the PR and its commit.
- *Where a PR is.* Search by PR number: approved at, inserted into which batch at which position, head attempts it has been through, whether and how its commit was modified by an agent (the appended explanation, the diff against the accepted head), landed at and the landing commit.
- *Per batch.* CI runs with runner, host, phase timings and verdicts per context; repair sessions with provider, model, duration, tool calls, tokens and cost at API list prices; which members each repair touched; the time from seal to landing split into rebase, CI and repair.
- *Recently landed.* The last N heads with size, cycle time, CI rounds, repair rounds, agent cost, members modified, members emptied; the daily journal report's numbers beside the Bors baseline.
- *Health.* The conditions and alerts from `/status`, the pass budget and snapshot staleness, the approval sources, the provider chain's recent choices and rate-limit state, the worker heartbeat.

Source of truth is the service's status and feeds plus the repair worker's task outcomes (`worker.json`, per-task `outcome.json` with `agent_usage`); the site should be a static or near-static render of those, reachable from the same hostname, with no write path. Visualisations earn their place only where they compress something a table cannot (the batch strip, the cycle-time split); everything else is a table with links.

## Later increments

These specifications are retained for adoption when increment 1's measurements show the need. They do not change the increment 1 behaviour above.

### Sealed successor batches and background CI

The batch queue becomes a sealed prefix followed by an unsealed suffix, with a runtime cap on unsealed batches (initial value 4) and a normal target on sealed batches including the head (initial value 2). Sealed non-head batches can run CI and receive agent repair before reaching the head, controlled by a runtime flag; background results apply to the source tested, and after a new base is integrated the candidate needs its own results. Background work operates on independently based batches and proactively rebases them toward current `main`, coalescing repeated advances into a pending refresh. Head repair and validation take priority over background work. A CI backlog threshold (initial value 100 queued jobs on the runner pools used by staging, counting jobs rather than PRs, with zero assumed when the observation is stale or unavailable and the assumption reported) permits new background launches; it never stops the head's necessary build. When the head lands, promote the successor and replenish the sealed prefix as capacity permits; prior background work reduces preparation time but does not establish the final candidate's green status. Only the first unsealed batch may be sealed, preserving the sealed-prefix invariant; a borrowed batch is marked to seal on release without preempting the current attempt.

### Combination and cap pressure

Unsealed batches are combined programmatically only when the queue is up against its unsealed cap: borrow both without deadlock, attempt a clean combination of their commit series, and adopt the result atomically, preserving membership, attribution and messages; on conflict leave both intact and launch no agent. When the cap is reached and combination cannot free a slot, seal the first unsealed batch even beyond the sealed target, logging batch identity, sealed/unsealed counts before and after, effective limits, the backlog observation and the reason combination failed, with a distinct counter for target overflow exposed in status. Capacity pressure must not make approved work disappear or create an unlimited number of empty branches.

### Agent-assisted insertion

If an insertion cannot be made cleanly on any available batch, an agent can borrow one unsealed batch and incorporate the PR as one additional constituent commit, editing other constituent commits where needed with append-only explanations. A borrowed insertion reserves that PR until completion or recovery.

### Clean-replay comparison for comments

Post a PR comment only when the final contribution differs from the clean result of rebasing/squashing its accepted branch onto its actual predecessor, so that an agent edit that turns out to be a no-op produces no comment. When a clean replay baseline cannot be produced, retain the agent-edit provenance rather than pretending to establish an exact comparison.

### Agents in GitHub Actions

Running agents as GitHub Actions jobs with an API key stored as a repository secret: that job holds a credential while reading PR-authored text and running tools, so it needs an egress allowlist limited to the model API and GitHub, a spend-capped key, and the stated assumption that PR text is untrusted input. Credentials and trusted workflow code remain outside candidate-controlled builds; ordinary source checks keep their existing isolation. A worker and an Actions job share the same claims.

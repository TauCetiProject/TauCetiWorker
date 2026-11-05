# Queue controller

`tauceti_queue/controller.py` is the state machine of increment 1 of
[`docs/redesign/specs/ai-merge-queue.md`](../redesign/specs/ai-merge-queue.md) (on the `overhaul`
branch): open batches receiving scripted insertions (one, plus a second for inputs that conflict with
the first; see "Open batches" below), at most one sealed head that is rebased, built, repaired in
place by agents and fast-forwarded onto the canonical branch. It is one object
shared by the reconciliation loop (`tauceti_queue/loop.py`) and the HTTP server (work plan stream F),
over one SQLite database (`tauceti_queue/store.py`) and a local clone of the content repository. It
talks to GitHub only through the `GitHubPort` protocol in `tauceti_queue/ports.py`; all chain work is
[`tauceti_queue/chain.py`](chain.md). `tauceti_queue/fakes.py` provides a fake GitHub over a bare
repository, a scripted CI and scripted agents for the local end-to-end fixture
(`tests/queue_fixture.py`) and for the cutover rehearsal.

## Storage

One SQLite file in WAL mode, stdlib `sqlite3`, every mutation inside a `BEGIN IMMEDIATE`
transaction. The plan names one database and PostgreSQL on the host; the service is a single
process (loop thread plus HTTP server sharing the `Controller`), so a database server adds nothing
but an operational dependency, and the operator prefers the simpler deployment. `Store` keeps all
the SQL (plain column types, `?` placeholders, JSON in TEXT columns) behind a small row API
(`insert`, `update`, `rows`, `row`, `count`, `transaction`, settings and journal helpers), so a
PostgreSQL backend is a second implementation of that class, not a change to the controller.

Tables:

| table | content |
| --- | --- |
| `settings` | runtime settings and controller state (`state:*` keys: canonical SHA, inference hold, counters, Bors's external head, the last approval poll) as JSON, with actor and time |
| `inputs` | approved inputs: repository, PR, accepted head, title, description, author, evidence, reviewer, priority, urgent flag, state, age (`approved_at`), retained ref, probe history (`probes`, `last_probe`, `last_probe_tip`, `passes_waited`) |
| `batches` | identity, version, base, tip, sealed flag, state (`open`, `head`, `landed`, `removed`), expected parent for promotion, urgent flag, first dispatch time |
| `members` | per batch and position: input, `Source-PR`, accepted head, current commit, provenance (`clean` or `edited`), the task that edited it, the accumulated explanation, every edited commit SHA |
| `claims` | batch identity and version, operation (`insert`, `rebase`, `task:repair`, `task:rebase_conflict`), worker identity and GitHub login, token, starting tip, expiry, renewal, release and its reason |
| `tasks` | repair and rebase-conflict tasks: batch and version, base, tip, `onto`, diagnostics, state (`open`, `submitted`, `superseded`), counts of claims and submissions |
| `submissions` | every submitted chain with its retained ref, accepted flag and reason |
| `ci_attempts` | every dispatch: head and base SHA, dispatch id, bound run id and attempt, status, conclusion, runner, labels, host, phases, progress timestamps, outcome (`pending`, `pass`, `source-failure`, `infrastructure`, `superseded`) and diagnostics |
| `outbox` | pending side effects (`promote`, `comment`, `close_pr`, `labels`, `retain_ref`) with an idempotency key, attempt count, history and next attempt time |
| `journal` | append-only log of what the controller did (`approved`, `inserted`, `sealed`, `rebased`, `dispatched`, `ci-pass`, `task`, `claimed`, `adopted`, `landed`, ...) for status and tests |

Retained refs in the clone and, when publication is enabled, on GitHub through the outbox:
`refs/tauceti-queue/inputs/<pr>/<accepted sha>` for every accepted head,
`refs/tauceti-queue/batches/<batch>/v<version>` for every adopted tip,
`refs/tauceti-queue/batches/<batch>/submissions/<sha>` for every submitted chain (accepted or not),
and `refs/tauceti-queue/submissions/<task>/<claim>` is where a worker pushes its result.
Retained refs are pushed through the outbox only while `publication_enabled` is true, so a ref
retained in shadow mode exists in the clone alone; `_republish_retained_refs` (at restart, when
publication is switched on, and once per tick) enqueues, for the head and every open batch, the
batch's tip under its current version and each non-removed member's accepted head when the clone
holds the ref and the outbox has no entry for that ref and SHA (done entries are kept, so the check
is one indexed lookup per ref and no GitHub call). The outbox pushes several retained refs in one
`git push` (`update_refs`), falling back to one push each when the batched push fails, and a
dispatch pushes the head's batch ref itself only when the outbox has not already done so.

## State machine

Inputs: `waiting` -> `reserved` (during a probe) -> `inserted` (a member) -> `landed`; or
`cancelled` with a reason (`unapproved`, `cancelled`, `closed`, `head-changed`, `draft`,
`merged-externally`, `landed-externally`, `conflicts-with-main`, `superseded-by-new-approval`).
A member is never withdrawn: `unapprove`, `cancel`, a closing or relabelling event on a member PR
are journaled (`member-event-ignored`, `withdrawal-refused`) and change nothing.

Batches: `open` -> `head` (sealed) -> `landed`; an empty open batch whose base is stale is
`removed` and recreated on demand. There is at most one open and at most one head batch.

Every `tick()` (one reconciliation pass) does, under the controller lock:

1. Expire claims; drain the outbox.
2. Fetch the canonical branch into the clone. If it advanced: a head whose tip is now an ancestor
   of it is `landed` (`promoted` when a promotion was recorded, `external` otherwise, which is how
   the shadow period and an admin merge of the whole head reconcile); waiting inputs whose
   `Source-PR` appears in the new commits are cancelled as `landed-externally`. A member PR landed
   outside the queue (an admin merge, or Bors during the shadow period) is never withdrawn: at the
   next rebase `chain.replay` finds its PR in the new commits since the old base (a subject ending
   ` (#N)` or a `Source-PR` trailer) and writes an empty commit with the member's message plus the
   note "Landed on main as <sha> outside the queue; nothing left to apply." instead of cherry-picking
   it, so the member cannot conflict with the changes that landed beside it; `chain.build` does the
   same for a rebuild. The replay result, the `rebased`, `rebase-conflict` and `rebuilt` journal
   entries and a `rebase_conflict` task's diagnostics list these members under `landed_on_base`
   (`{source_pr, commit}` each), and the repair prompt states them as "Already on main".
3. Insertion passes: while there are waiting inputs not yet probed in this pass against an open
   batch they are eligible for, ensure the first open batch exists (based on the canonical commit),
   pick the oldest open batch with eligible inputs, draw one (below), reserve it, claim the batch,
   `chain.build` its squash onto the tip in the clone, recheck the batch version, and commit tip,
   version, member row, retained ref and the `inserted` state in one transaction (plus the
   membership label through the outbox). A conflict leaves the input waiting with the paths and,
   per batch (`probe_conflicts`), the base it conflicted against: the input is not probed against
   that batch again while its base is unchanged (the conflict is with content already in the
   chain, so appending members cannot resolve it) but against the next open batch, created when
   none exists and fewer than `open_batch_limit` are open; a conflict against the canonical commit
   itself cancels the input and posts the conflict request. Any error returns the input to waiting
   and releases the claim. Empty open batches beyond the first are removed at the end of the pass.
4. Seal: when there is no head, the oldest open batch is nonempty, no claim is active on it, no
   inference hold is in force and Bors's external head (below) is not awaited, it becomes the head
   in the same pass; the other open batches stay open and keep receiving insertions, and a new open
   batch is created only when none remains. There is no collection window.
5. Head reconciliation, in the plan's order: a head under a worker's task claim is left alone;
   a head not based on the canonical commit is claimed and replayed (`chain.replay`), a clean
   replay becoming the next version with provenance unchanged, a conflict becoming a
   `rebase_conflict` task with the conflicting member, paths and the clean prefix as evidence;
   a head with an open task waits for a worker; otherwise CI (below) and, on a pass, promotion
   through the outbox.
6. Enqueue the live batches' retained refs that never reached GitHub (above); drain the outbox again.

Insertion draw: inputs are ordered urgent first, then priority, then age. The window is the
`insertion_age_window` first. An urgent input in the window is drawn; else an input that has waited
`insertion_age_window` passes while in the window is drawn (oldest first), which is the starvation
bound of scenario 18; else one is drawn at random. `passes_waited` counts passes spent in the window
without being drawn and resets on a draw.

Open batches: increment 1 has one open batch, and an approved PR whose replay conflicts textually
with content in it waits until that batch seals and lands. A step beyond increment 1 (operator
decision, 2026-11-05, with about 190 approved inputs waiting behind a 1,200-member open batch in
production) allows up to `open_batch_limit` open batches (default 2; 1 restores increment 1), ordered
by id. An input is probed against the first open batch; one whose recorded conflict is against that
batch's current base is probed against the next, which is created on the canonical commit (never on
another batch's tip: batches are never stacked on an unlanded predecessor) when none exists. An
input that conflicts with every open batch waits as before. Sealing takes the oldest open batch;
when it lands and the canonical commit advances, the next sealed head is rebased by the ordinary
path and its conflicts become `rebase_conflict` tasks for the agent, which is the intended place for
them. The urgent lane still inserts into the first open batch, which seals when the head lands.

CI: the attempt for the head's current version is dispatched with `dispatch_candidate` (the
`repository_dispatch` of `pr-build.yml`, event `tauceti-bors-staging`, payload `head_sha`,
`base_sha`, `batch_id`, `members`, `base_ref`). Observation reads `find_candidate_run` when an event
named the head or `polling_interval_seconds` elapsed, recording runner, host, phases and progress
times. The verdict is the `build`, `bump-guard` and `scope` commit statuses on the tip whose `run_id`
is the run that completed for this attempt, all three `success` (a red `scope` or `bump-guard` beside
a green `build` is a source failure carrying the status description); anything else (no run within `dispatch_timeout_seconds`, no progress for
`lost_run_seconds`, a cancelled, timed out or skipped run, a completed run without a bound status, a
status from another run, a failure whose description starts with `infrastructure` or whose findings
are all of kind `infrastructure`) is an infrastructure outcome: the candidate is unchanged and is
re-dispatched after `infrastructure_retry_backoff_seconds * 2^n`, at most
`infrastructure_retry_limit` times, after which the head waits and status says so (`retry_ci` is the
operator's reset). A failing status with source findings is a source failure and opens a `repair`
task carrying the findings. A pass enqueues the promotion.

Promotion: an outbox entry `promote:<batch>:<version>:<tip>` that fast-forwards the canonical
branch from the recorded expected parent to the tip (no merge commit). Before pushing, and on every
retry after a transport failure, the canonical branch is re-read: if it already contains the tip
the head is landed (a lost response). A refusal with another parent returns the head to the rebase
step. Landing marks the batch and its inputs, releases claims, supersedes tasks, and enqueues per
member the landing comment and the close.

Landing side effects replicate bors-ng's squash landing exactly (`lib/worker/batcher.ex`,
`complete_verified_batch` and `close_merged_pr`): a comment "Pull request successfully merged into
`<branch>`." with the build status and the merged revision, carrying an idempotency marker, and a
`PATCH pulls/<n> {state: closed, title: "[Merged by Bors] - <title>"}`. Labels are left as they are
(Bors leaves them as the record of how the PR merged); the queue's own membership label
(`membership_label`, default `bors-staging`) is added at insertion. The `Source-PR` trailer in the
landed commit is the landing evidence; GitHub does not show the PR as merged.

Provenance and comments: at adoption, `chain.compare` pairs the prior chain with the replacement by
`Source-PR`. A member reported `edited` becomes provenance `edited` with the adopting task, the
appended explanation accumulated onto earlier ones and the commit SHA added to `edited_shas`; a
member reported `clean` keeps its provenance (a mechanical replay after an earlier edit is not a
new edit, and an earlier edit is not forgotten by a later clean replay). The landing comment of an
edited member quotes the accumulated explanations, links the landed commit and every edited
revision, lists the PRs the explanation mentions, and names the retained ref; a clean member's
comment is Bors's notice only. Comment publication is an outbox entry retried independently of the
landing.

Inference hold: workers report provider outcomes through `report_inference(worker, ok, error)` and
`release_task(..., reason="inference_unavailable", error)`. `inference_failure_threshold`
consecutive failures start a hold (`since`, `last_error`, `reports`, `attempts`, `next_retry_at`);
further failures update the error and, once `next_retry_at` has passed, double the backoff up to
`inference_hold_backoff_max_seconds`. While held the controller does not seal; insertion, CI
observation and promotion continue (none of them needs inference), the head keeps its membership
and retained source, and tasks stay claimable by any authorized worker. The hold ends on the first
successful report or adopted task; the status reports it, and an alert names a hold older than an
hour.

Urgent lane: `mark_urgent` flags the input (drawn next, ahead of the random draw) or, if it is
already a member of the open batch, the batch. The open batch seals as soon as the head lands in
any case; the running head is never cancelled.

Bors's state at the cutover (`import_bors_state(observation, candidates, now=, dry_run=,
release_head=)`, implemented in `tauceti_queue/bors_import.py`): every member of a non-running batch
in Bors's observation feed is approved at its head through `approve(..., approved_at=, pull=)` with
the age, reviewer and priority the candidate journals record for that (PR, head) (`original_admitted_at`
in unix microseconds, `reviewer`, `scheduling.priority`; the import time and the review App when
unknown) and evidence `{"source": "bors-import", "batch_id", "feed_observed_at", "batch_state",
"sealed", "age_source", "reviewer_source", ...}`; an administrator's `manual_approval` becomes the
`admin`, `actor_id` and `merge_base_sha` evidence the observation feed's `scope_approvals` reads.
Each live PR is read once (`validate_pull`) and refused with `closed`, `draft`, `head-mismatch`,
`base-branch`, `fetch-failed: ...` or `head-unavailable`; the heads are prefetched in chunks of pull
refs. The members of the running batch are recorded as the external head (`state:external_head`:
batch, head and base SHA, members, `landed_members`, state `running`, `landed` or `released`), not
as inputs, and `_seal` refuses while it is `running`, with the reason in status. Landing is observed
on a canonical advance: a commit without a `Source-PR` trailer whose subject ends in `(#N)` is Bors's
squash of PR N, the head's tip becoming an ancestor of the canonical commit lands every member, and a
`pull_request` `closed` event carrying Bors's `[Merged by Bors] - ` title lands that member; the head
is `landed` once every member has. A re-import whose feed no longer runs the batch, or
`release_head`, marks it `released` and approves its unlanded members with their ages. Re-running
imports nothing already present at the same head (and reads no PR for it); a dry run validates and
reports without writing. The import refuses to start when fewer REST calls remain than PRs to read.

Approval intake by polling (`tauceti_queue/approval_poll.py`): `ApprovalPoller.run_once` lists the
open PRs with their `merge eligibility` checks (`GitHubPort.list_open_pull_requests`) and calls
`eligibility.reconcile` for each decision the controller has not recorded (an eligible check at a
head without a live input, an unsafe review on a PR with one), `handle_event("pull_request", ...)`
for a waiting input whose PR moved, closed or became a draft (the webhook path's actions, so members
are journaled and kept), each decision once. `note_approval("webhook" | "polled")` counts the
source; `record_approval_poll(summary)` keeps the last pass for status. `ApprovalPollLoop` runs a
pass at start, every `approval_poll_interval_seconds` and on wake.

Restart: `recover()` releases the controller's own in-process claims (`insert`, `rebase`), returns
`reserved` inputs to `waiting`, expires overdue claims, enqueues the live batches' retained refs
that never reached GitHub and leaves worker claims, tasks and the outbox as they are; the first pass
then drains the outbox before any new work. `ReconciliationLoop`
calls it once, then runs a pass whenever the controller's wake event is set (webhooks, commands,
task calls) and otherwise every `polling_interval_seconds`, skipping polling passes while the API
quota is below `rate_limit_reserve`.

## Settings

Changed at runtime with `set_setting(key, value, actor)`, journaled, effective at the next decision
and reported in status:

| setting | initial | meaning |
| --- | --- | --- |
| `insertion_age_window` | 16 | size of the age window the draw is taken from |
| `open_batch_limit` | 2 | open batches at most; an input conflicting with the first is assembled into the next (1 restores increment 1) |
| `repair_concurrency` | 1 | concurrent task claims on the head |
| `stall_interval_seconds` | 900 | build silence after which status raises a stall |
| `publication_enabled` | false | shadow mode when false: no dispatch, promotion, comment, label, close or ref push; status says what would happen |
| `claim_ttl_seconds` | 900 | claim lifetime between renewals |
| `dispatch_timeout_seconds` | 600 | no run found after a dispatch: infrastructure |
| `lost_run_seconds` | 3600 | no progress in a run: lost runner |
| `infrastructure_retry_limit` | 3 | re-dispatches per head version after infrastructure failures |
| `infrastructure_retry_backoff_seconds` | 60 | base of the re-dispatch backoff |
| `inference_failure_threshold` | 3 | consecutive provider failures that start a hold |
| `inference_hold_backoff_seconds`, `inference_hold_backoff_max_seconds` | 60, 3600 | hold backoff |
| `outbox_retry_seconds`, `outbox_retry_max_seconds` | 30, 900 | outbox retry backoff |
| `polling_interval_seconds` | 60 | polling fallback interval |
| `approval_poll_interval_seconds` | 900 | interval of the approval reconciliation poll behind the webhooks |
| `membership_label` | `bors-staging` | label added to member PRs at insertion |
| `insertion_probe_budget_seconds` | 20 | wall-clock budget of one tick's insertion probes; the rest wait for the next tick, which follows at once |
| `outbox_drain_budget_seconds` | 20 | wall-clock budget of one tick's outbox drain; the rest wait for the next tick, which follows at once |

## Ports

`GitHubPort` (`tauceti_queue/ports.py`), implemented by stream F's adapter and by `FakeGitHub`:
`fetch_pr`, `fetch_refs` (into the service's clone), `dispatch_candidate`, `find_candidate_run`,
`commit_status`, `fast_forward`, `update_ref`, `post_comment` (idempotent on a marker),
`set_labels`, `close_pr`, `repository_admin`, `rate_limit`, and for approval intake
`eligibility_checks(repo, head)`, `pr_merge_base(repo, number, head_sha)` (what
`eligibility.reconcile` reads) and `list_open_pull_requests(repo, review_app_id=)` returning
`OpenPullRequest` records (number, head, base, draft flag, the review App's check runs on the head)
for the approval poll. `close_pr` is the one method beyond the agreed list: Bors closes a
squash-landed member with a title change, which no other method expresses. `CandidateRun.diagnostics` carries the findings the adapter extracted from the run
(dicts with `kind`, `module`, `message`); the controller passes them to the repair task and uses
only `kind == "infrastructure"` for classification.

Commands arrive from the adapter as `approve(repo, pr, head_sha, reviewer, priority, evidence,
approved_at=None, pull=None)`, `unapprove`, `cancel`, `set_priority`, `mark_urgent(repo, pr, actor)`;
`approved_at` lets the cutover import keep an input's original age and `pull` a PR already read.
`validate_pull(repo, pr, head_sha)` and `input_for(repo, pr)` serve the import and the poll. Events arrive as
`handle_event(kind, payload)` with the raw webhook payload; they refresh what they describe
(`push` to the canonical branch, member-head `synchronize`, closes, drafts, CI events naming the
head) and wake the loop. `retry_ci(repo, actor)` and `set_setting` are the administrative calls.

The controller serves one repository; the service runs one controller per opted-in repository.

## Task API

Called by stream F's HTTP handlers on behalf of workers (stream G). Authorization is the existing
predicate, repository admin permission (`security_audit.require_admin`), checked through
`repository_admin(login)` at claim and again at submit.

- `claim_task(worker_identity, github_login) -> Task | None`: the head's open task under a new claim
  (`claim_token`, `expires_at` = now + `claim_ttl_seconds`) when the login is an admin and fewer
  than `repair_concurrency` claims are active on it. The `Task` carries kind, batch and version,
  `base` and `tip` (the chain to start from), `onto` (the commit the replacement must be based on:
  the canonical commit for a rebase conflict, the base itself for a repair), members with accepted
  head, current commit, provenance, title, description, author, explanation and retained ref,
  diagnostics, retained refs, `submission_ref` (where to push the result) and the agent instruction.
- `renew_task(task_id, claim_token) -> expires_at`; `release_task(task_id, claim_token, reason,
  error)`. Both raise `TaskError` (`unknown-claim`, `claim-expired`, `claim-superseded`) when the
  claim is not live.
- `submit_task(task_id, claim_token, tip_sha, worker_notes) -> Adoption`: fetches the tip if it is
  not in the clone, retains it, then rejects in this order: `claim-expired`, `claim-superseded`,
  `not-authorized`, `version-mismatch` (the task is no longer the head's current task), `tip-unavailable`,
  `invalid-chain` (`chain.validate` against the recorded members on `onto`, then `chain.compare`
  against the prior chain: single-parent history, the member bijection by identity, append-only
  messages), `explanation-required` (content changed without an appended explanation; the same
  claim resubmits with it). Acceptance records the new version, base `onto`, tip, member commits
  and provenance, supersedes other claims and tasks on the batch, retains the tip under
  `.../v<version>`, lifts any inference hold, and the next pass dispatches CI. A rejected chain stays
  retained under `.../submissions/<sha>`.

## Status

`status()` returns the `tauceti-queue.status/v1` document, JSON-serialisable, with: `repository`,
`canonical_branch`, `canonical_sha`; `service` (running revision, start time, now, last pass);
`settings` (effective values) and `publication_enabled`; `inference_hold`; `waiting` (count, urgent
count, oldest age, max passes waited, the inputs); `open_batch` (the first open batch: id, version,
base, tip, size, oldest member age, members, claim, retained ref); `open_batches` (every open batch
in id order: id, version, base, tip, size, urgent, oldest member age); `head` (the same plus `sealed_at`,
`expected_parent`, `based_on_canonical`, `stage` in `rebase`, `rebase_conflict`, `dispatch`, `ci`,
`repair`, `infrastructure`, `promote`, the open `task`, and `ci` with dispatch id, run id and
attempt, status, conclusion, timestamps, runner, labels, host, phases with timestamps, last progress,
outcome and detail, plus `stall` with `since`, `silence_seconds` and the phase when the build has
been silent for `stall_interval_seconds`); `external_head` (Bors's head awaited after the cutover:
batch, head and base SHA, state, member counts, `pending_members`, timestamps); `approvals`
(`webhook`, `polled`, `revoked_by_poll`, `last_poll`); `tasks`; `outbox` (pending entries with attempt history);
`reasons` (why the last pass is waiting); `alerts` (the first-line conditions: a stall longer than
two intervals, a hold older than an hour, two chain validation failures on one head, a failing
promotion); `counters` (heads sealed and landed, PRs landed and edited, dispatches and
re-dispatches, passes and source failures, repair rounds, rebase conflicts, shrink-to-empty events,
claim collisions, chain validation failures, inference holds, promotion refusals); the recent
`head_cycle_seconds` (first dispatch to landing); `insertion_passes`; `max_passes_waited`.

`status()` waits at most two seconds for the controller lock (`wait=` overrides; `/healthz` passes
half a second). When a pass holds it longer, the snapshot taken at the end of the last pass or
operator action is returned with `snapshot: true` and its age in `stale_seconds`; before the first
pass completes, a document with only the service facts. A live read carries neither key.

## Scenario to test mapping

Tests are `tests/queue_fixture.py` (F), `tests/queue_controller.py` (C) and `tests/queue_store.py`.

| scenario | test |
| --- | --- |
| 1 clean insertion, rebase, CI, publication, zero agents | F `test_s1_clean_insertion_rebase_ci_and_publication_without_agents` |
| 2 conflict or CI failure gives a narrow task and a validated replacement | F `test_s2_s4_ci_failure_yields_repair_task_and_validated_replacement_with_explanations`, `test_s2_rebase_conflict_yields_task_and_replacement_chain` |
| 3 single-parent bijective chains with empty commits; order-free validation | F `test_s3_every_commit_single_parent_bijective_including_empty`, `test_s3_validation_accepts_membership_equivalent_replacement_in_another_order` |
| 4 message prefix preserved, explanations appended, comments from messages | F `test_s2_s4_...`, `test_s4_edit_without_explanation_is_rejected_and_the_same_attempt_adds_it`; C `Comments` |
| 5 mechanical rebase makes no edit comment; edited comment retried independently | F `test_s5_mechanical_rebase_makes_no_edit_comment_and_comment_delivery_retries_independently` |
| 6 reservation, claims, expiry, late results | F `test_s6_s11_claims_expiry_late_results_and_second_claimants`, `test_s6_reservation_and_duplicate_approvals_cannot_insert_a_pr_twice`; C `Tasks` |
| 7 one open batch, at most one head, claimed batch protected, seal without delay, restart | F `test_s7_sealing_promotion_and_restart_keep_one_open_batch_and_at_most_one_head` |
| 8 open batch never launches CI; settings change without a PR | F `test_s8_open_batch_never_launches_ci_and_settings_change_without_a_pr`; C `Settings` |
| 9 a conflicting PR is probed again and inserts once its batch-mate landed | F `test_s9_conflicting_pr_is_probed_again_and_inserts_once_its_batch_mate_landed`, `test_conflicting_pr_is_not_probed_again_until_the_batch_base_changes` (both with `open_batch_limit` 1), `test_pr_conflicting_with_main_itself_gets_the_conflict_request` |
| 10 admin advances, lost promotion responses, no merge commit, stale CI | F `test_s10_admin_advance_...`, `test_s10_promotion_refused_after_an_advance_between_pass_and_push`, `test_s10_lost_promotion_response_is_reconciled_from_main`, `test_s10_s19_stale_status_cannot_approve_changed_source` |
| 11 authorized claim, second claimant and late result cannot compete | F `test_s6_s11_...`, `test_s11_renewal_keeps_a_claim_alive_and_release_frees_the_task`; C `test_repair_concurrency_allows_parallel_claims_and_first_adoption_wins` (warm artifacts are stream G's) |
| 12 restart recovers outbox, claims and CI state before repeating work | F `test_s12_restart_recovers_pending_outbox_expired_claim_and_repeats_no_work`, `test_s12_loop_runs_passes_on_wake_and_on_the_polling_interval` |
| 13 inference hold | F `test_s13_inference_hold_stops_sealing_keeps_the_head_and_resumes_on_success`, `test_s13_hold_without_a_head_prevents_sealing_until_a_provider_call_succeeds`; C `Holds` |
| 14 shrink to green | F `test_s14_shrink_to_green_lands_with_an_empty_commit_and_names_what_was_dropped` |
| 15 live activation | not expressible locally; the rehearsal (`docs/queue/rehearsal.md`) covers the scratch-repository half of the transition |
| 16 post-admission problem reports do not withdraw | F `test_s16_s21_problem_reports_closures_and_relabelling_do_not_withdraw_a_member` |
| 17 two source failures, no singletons, at most three full runs | F `test_s17_two_source_failures_land_with_three_full_runs_and_no_singletons` (counts only; the 120-minute bound is a pilot measurement) |
| 18 no PR waits more than the window in passes | F `test_s18_no_pr_waits_more_than_the_window_in_passes`; C `Draw` |
| 19 PR events and pushes neither cancel nor verdict; cancelled runs change nothing | F `test_s19_pr_events_and_branch_pushes_never_cancel_a_candidate_or_post_its_verdict`, `test_s10_s19_stale_status_...` |
| 20 urgent lane | F `test_s20_urgent_pr_is_in_the_next_head_without_cancelling_the_running_one` |
| 21 closing or relabelling a member changes nothing | F `test_s16_s21_...`; C `test_waiting_inputs_react_to_events_and_members_do_not` |
| 22 status names runner, host, phases, stall, revision | F `test_s22_status_names_runner_host_phase_progress_stall_and_revision`; C `StatusShape` |
| 23, 24 increment 0 and transition | not expressible here; `tests/queue_rehearsal.py` runs the cutover rehearsal's scenario script over these fakes (`docs/queue/rehearsal.md`); `tests/queue_bors_import.py` covers the import of Bors's state over a reduced production feed and the first-head rule; `tests/queue_approval_poll.py` the approval poll |

Also covered: infrastructure failures retried with backoff and bounded
(`test_infrastructure_failures_are_retried_with_backoff_and_bounded`), shadow mode publishing
nothing and reconciling an external landing (`test_shadow_mode_builds_batches_but_publishes_nothing`). The second open batch (beyond increment 1) is F `OpenBatches`: a conflicting PR is
assembled into a second batch based on the canonical commit, the oldest seals first and the other
keeps receiving, `open_batch_limit` 1 keeps it waiting, a PR conflicting with every open batch
waits, an empty second batch is removed, and a restart attributes a legacy conflict record.

# Queue controller

`tauceti_queue/controller.py` is the state machine of increment 1 of
[`docs/redesign/specs/ai-merge-queue.md`](../redesign/specs/ai-merge-queue.md) (on the `overhaul`
branch): open batches receiving scripted insertions (one, plus a second for inputs that conflict with
the first; see "Open batches" below), sealed batches prepared by a background worker,
and one head that is rebased, built, repaired in place by agents and fast-forwarded onto the canonical branch. It is one object
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
| `inputs` | approved inputs: repository, PR, accepted head, title, description, author, evidence, reviewer, priority, urgent flag, state, age (`approved_at`), retained ref, probe history (`probes`, `last_probe`, `last_probe_tip`, `passes_waited`), the `no_repair` opt-out and, for a `problem-anticipated` withdrawal, `anticipated` (JSON: batch, member, commit, `interacting`, `file`, `finding`, the agent's `explanation`, `stage`, times) |
| `batches` | identity, version, base, tip, sealed flag, state (`open`, `head`, `landed`, `removed`), expected parent for promotion, urgent flag, first dispatch time, `lane` (`control` for the control-file lane batch, else empty) |
| `members` | per batch and position: input, `Source-PR`, accepted head, current commit, provenance (`clean` or `edited`), the task that edited it, the accumulated explanation, every edited commit SHA, the `no_repair` opt-out, `withdrawn` (a member the queue sent back: an emptied `no_repair` member, or one an adopted repair emptied; its empty commit stays in the chain, the PR does not land) |
| `claims` | batch identity and version, operation (`insert`, `rebase`, `task:repair`, `task:rebase_conflict`), worker identity and GitHub login, token, starting tip, expiry, renewal, release and its reason |
| `tasks` | repair and rebase-conflict tasks: batch and version, base, tip, `onto`, diagnostics, state (`open`, `submitted`, `superseded`), counts of claims and submissions |
| `batch_tips` | every tip a batch has had: batch, version, tip SHA and the time it was recorded (written wherever the tip changes: insertion, adoption, rebase, rebuild, and at dispatch for a version not yet recorded) |
| `submissions` | every submitted chain with its retained ref, accepted flag and reason |
| `ci_attempts` | every dispatch: head and base SHA, dispatch id, bound run id and attempt, status, conclusion, runner, labels, host, phases, progress timestamps, outcome (`pending`, `pass`, `source-failure`, `infrastructure`, `superseded`), diagnostics and the `repair_outputs_revs` the dispatch sent |
| `outbox` | pending side effects (`promote`, `comment`, `close_pr`, `labels`, `retain_ref`, `notify`) with an idempotency key, attempt count, history and next attempt time |
| `failures` | history only, no longer written: one row per failed head CI attempt from when a recurring failure could withdraw members or bisect the head; see [escalation.md](escalation.md) |
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
`merged-externally`, `landed-externally`, `conflicts-with-main`, `superseded-by-new-approval`,
`problem-anticipated`, `empty-commit`, and `queue-withdrawn` from before the escalation ladder was
removed). A failing head goes to repair however often it fails; the queue never withdraws, bisects or
evicts a member on its own (see [`escalation.md`](escalation.md)). An ordinary member an adopted repair reduces to an empty commit
is withdrawn at adoption with reason `empty-commit` (`_withdraw_member`; see
[`reject-empty-commits.md`](reject-empty-commits.md)).
A member is never withdrawn by its author or reviewers: `unapprove`, `cancel`, a closing or
relabelling event on a member PR are journaled (`member-event-ignored`, `withdrawal-refused`) and
change nothing, except that a `labeled`/`unlabeled` event for one of the `no_repair_labels` updates
the member's `no_repair` flag (journal `no-repair`).

The one withdrawal of a member is the opt-out's: when an adopted chain has reduced a `no_repair`
member to an empty commit (and the PR is not simply already on `main`), the member is marked
`withdrawn` and its input `cancelled` with reason `problem-anticipated` (journal
`problem-anticipated`, counter `no_repair_withdrawals`). The member stays in `_members` so the
chain keeps its bijection, and its empty commit travels with the batch, through rebases, until the
head lands; landing skips it (not marked landed, no comment, no close; `landed` journals it under
`withdrawn`), a canonical advance does not count its commit as landing the PR, and `chain.landed_prs`
ignores empty commits in general, so the PR can still land later. `anticipated.stage` then moves on:
`pending` while the interacting PR is followed (`unattributed` when the agent named none, which
waits for a new approval); `needs-rebase` when the interacting PR lands (head landing or canonical
advance: comment, label, journal `needs-rebase`, counter `problem_anticipated_needs_rebase`; the PR
re-enters by re-approval); `readmitted` when it leaves the queue without landing (input `waiting`
again at its original `approved_at`, journal `readmitted`, counter `problem_anticipated_readmitted`),
or `readmit-refused` when the PR is no longer open at its accepted head or carries a keep label;
`superseded` when the PR was approved again meanwhile. Comment shapes: `docs/queue/github.md`,
"Repair or reject".

Batches: `open` -> `head` -> `landed`; an empty unsealed batch whose base is stale is
`removed` and recreated on demand. Sealing fixes membership independently of head status: the
a sealed non-head batch has `state=open, sealed=1`, preserving the existing background task
protocol. There is at most one head and a target of two sealed active batches in total, plus
`open_batch_limit` unsealed batches. When head promotion waits for an active preparation,
both sealed batches can temporarily be non-head batches.

Every `tick()` (one reconciliation pass) does, under the controller lock (except around its GitHub
I/O, below):

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
   none exists and fewer than `open_batch_limit` are unsealed; a conflict against the canonical commit
   itself cancels the input and posts the conflict request. Any error returns the input to waiting
   and releases the claim. Empty open batches beyond the first are removed at the end of the pass.
   Sealed successors and batches a `prepare` task holds (below) are skipped, so inflow goes on into
   another unsealed batch.
4. Seal: when there is no head, the oldest open batch is nonempty, no claim is active on it, no
   inference hold is in force and Bors's external head (below) is not awaited, it becomes the head
   in the same pass; the other open batches stay open and keep receiving insertions, and a new open
   batch is created only when none remains. There is no collection window. An active `prepare` or
   `pre_rebase` owner keeps the batch open until adoption, release or claim expiry, so completed
   repairs enter the new head through ordinary validation. Unclaimed background offers are
   superseded (resolution `sealed`); the new head keeps what its preparations adopted.
   The exception is a batch whose pre-rebase is a conflict that no worker has claimed
   (`_seal_clean_prefix`). A `pre_rebase` task still claimed at this point is current (made on the
   tip that landed) or explicitly preserved (`_supersede_stale_pre_rebase` has dropped any other),
   so it may still submit and the seal waits for it like any other owner; the next head reuses its
   result when the carry onto the canonical commit, the extension and the membership checks of
   `_seal_from_pre_rebase` succeed. Without an owner, resolving the conflict takes hours, so the
   batch is replayed onto the canonical commit and the members before the conflicting one move to a
   new batch that becomes the head at once (`clean-prefix-sealed` in the journal). The conflicting
   member and those after it stay in the batch, now based on the last moved member's own commit, so
   they keep their approvals, their age and the batch's place as the next head; the batch's
   unclaimed `pre_rebase` task is superseded (resolution `clean-prefix`) and the next pre-rebase
   onto the new head creates a fresh task. A conflict at the first member leaves the batch as
   before; a replay that is now clean seals the whole batch.
   While fewer than two active batches are sealed (counting the head), seal the oldest unsealed batch once it has
   `sealed_successor_min_members` members (default 100; 0 disables new successor sealing). A larger
   later batch never overtakes a smaller first one. Successor sealing preserves active background
   claims and versions, closes membership permanently, and makes preparation due immediately
   without a size or quiet-period gate. On promotion the successor retains its original `sealed_at`;
   replenish the sealed prefix from the next oldest eligible batch in the same pass. A claimed
   preparation can delay head promotion but cannot stop replenishing the two sealed batches.
   Head creation
   itself never waits for the successor threshold.
5. Head reconciliation, in the plan's order: a head under a worker's task claim is left alone;
   a head not based on the canonical commit is claimed and, when its pre-rebase (below) was made on
   exactly that commit, based there from the pre-rebased chain without a replay; otherwise replayed (`chain.replay`), a clean
   replay becoming the next version with provenance unchanged, a conflict becoming a
   `rebase_conflict` task with the conflicting member and commit, paths and the clean prefix as
   evidence (the worker replays the chain itself and resolves each conflict in turn,
   docs/queue/repair-task.md, "Rebase conflicts: one commit at a time");
   a head with an open task waits for a worker; otherwise CI (below) and, on a pass, promotion
   through the outbox.
6. Pre-rebase (below): supersede stale `pre_rebase` tasks; once the head's chain is submitted and
   waits for its verdict, replay the oldest open batch onto the head's tip, creating a `pre_rebase`
   task for a conflict only once the batch is prepared (below).
7. Background preparation (below): supersede `prepare` tasks whose batch sealed or changed;
   otherwise create the `prepare` tasks that are due, whatever the head is doing, except on a batch
   whose `pre_rebase` task a worker holds.
   (Between steps 2 and 3 the pass follows the `problem-anticipated` withdrawals: above.)
8. Enqueue the live batches' retained refs that never reached GitHub (above); drain the outbox again.

Insertion draw: inputs are ordered urgent first, then priority, then age. The window is the
`insertion_age_window` first. An urgent input in the window is drawn; else an input that has waited
`insertion_age_window` passes while in the window is drawn (oldest first), which is the starvation
bound of scenario 18; else one is drawn at random. `passes_waited` counts passes spent in the window
without being drawn and resets on a draw.

Open batches: increment 1 has one open batch, and an approved PR whose replay conflicts textually
with content in it waits until that batch seals and lands. A step beyond increment 1 (operator
decision, 2026-11-05, with about 190 approved inputs waiting behind a 1,200-member open batch in
production) allows up to `open_batch_limit` unsealed batches (default 2; 1 allows one), ordered
by id. An input is probed against the first open batch; one whose recorded conflict is against that
batch's current base is probed against the next, which is created on the canonical commit (never on
another batch's tip: batches are never stacked on an unlanded predecessor) when none exists. An
input that conflicts with every open batch waits as before. Sealing takes the oldest open batch;
when it lands and the canonical commit advances, the next sealed head is rebased by the ordinary
path and its conflicts become `rebase_conflict` tasks for the agent, which is the intended place for
them. The urgent lane still inserts into the first unsealed batch; it cannot extend the sealed successor.
A sealed successor does not consume unsealed capacity. Its membership remains closed after its
preparation finishes, including after a controller restart or disabling new successor sealing.

Missing-source insertion probes wait only for sources absent from the probed batch's base and tip
that another active batch introduces or the current canonical tree supplies. An older open batch
can therefore wait to refresh its base after a module lands. A head merely inheriting a module is
not a dependency if the proposed
PR removes that module, or imports it after a batch-mate removed it, landing the inherited copy
cannot repair the prospective tree. Those cases receive the ordinary missing-source request.
A genuinely new module in another batch still waits for that batch to land before re-probing.

A missing-source refusal is not final for its head. The refusal records a dependency fingerprint
(`evidence.missing_source_watch`): for each gap, the blobs at its importer and at its missing source
on the canonical commit, and the distinct such pairs at active batch tips that differ from it.
Whenever the canonical commit or an active batch's tip moves, `_recheck_missing_sources` recomputes
it for each PR's newest refusal. When it differs (the source landed, entered or left a batch, or an
importer changed), the refusal becomes `missing-source-recheck` (journal `missing-source-recheck`)
and admission (`readmit`) reads the PR again. If that read would admit the head (open, not a draft,
review-safe, scoreboard green, no blocking label), `restore_missing_source` returns the same input
to waiting at its original age (journal `missing-source-readmitted`, counter
`missing_source_rechecks`), and it is probed like any waiting input, which refuses it again if a
gap remains. The refusal comment is keyed by head, so a repeated refusal posts nothing new.
Closing, drafting, pushing to or withdrawing a refused PR drops its refusal from the recheck.

Refusals that need a new head go back to the PR's worker as a head-bound recovery request: a
comment beginning ``Merge-queue recovery for head `abc1234`: `` with a
`<!--tauceti-rebase:v1 HEAD-->` line for the refused head, then `needs-rebase` on and
`ready-to-merge` off. Both effects are one outbox entry (kind `recovery`), retried together and
dropped (`stale-head`) once the PR is closed or no longer at the refused head. The source adapter appends that marker itself and escapes any quoted
`<!--tauceti-` text, so only the request's own head is marked. A `conflicts-with-main` refusal and
an `empty-commit` withdrawal at adoption post the request at once, in place of their old comments.
A `missing-source` refusal keeps its ordinary comment and recheck, and gets the request only once it
has stood `missing_source_handoff_seconds` (default six hours) since its first refusal
(`evidence.missing_source_first_refused`; `_hand_off_missing_sources`, journal `missing-source-handoff`, once per head). The delay matters
on Forgejo, where the queue's own recovery request holds the head in admission: the recheck gets
the early window, when a provider is most likely to land, and the worker gets the PR when nothing
has. On GitHub the queue is not a recovery-hold author, so the recheck can still re-admit the head
afterwards. GitHub workers recognize `tauceti-queue[bot]` requests through TauCetiTools'
`rebase_authors`, and GitHub's status labels report such a head as `awaiting-author`; Forgejo
workers trust the user IDs in `TAUCETI_FORGEJO_RECOVERY_AUTHOR_IDS` (the queue's publisher).
A push makes every request stale.

A blocking label (`hold`, `wip` and the others `approve` refuses) added to a PR whose input is
waiting or reserved is recorded in the input's `blocked_labels` (journal `blocked-label`), from the
`labeled` delivery or, when that was missed, from admission's or eligibility's next read of the PR.
While it is set the input stays waiting but no batch takes it: ordinary insertion, the control lane
and the emergency lane skip it, and a probe that was already running returns it to waiting
(`last_probe` `blocked-label`). It keeps its approval and age, so once the labels go (journal
`blocked-label-cleared`) it is placed like any waiting input. A batch member is not touched.
Refusals recorded before the fingerprint existed compare against the base of the batch that
refused them, without batches.

Background preparation (item 8 of "Pipeline acceleration behind the head" in the plan): while the
head waits on CI or is being repaired, an open batch is built as assembled on its own base (never on
the head's tip) and its internal failures are repaired by an agent, so that it is ready when it
seals. When `prepare_open_batches` is on, every open batch with at least `prepare_min_members`
members, no insertion for `prepare_quiet_seconds`, a `prepared_version` other than its version, and
no `prepare` task open or settled (adopted, clean or failed) at that version gets one `prepare`
task: `base` and `tip` are the batch's, `onto` is `base`, diagnostics are empty. The batch lands on
`main`, so one based elsewhere is first replayed onto the canonical commit: a clean replay re-bases
it (a new version) and it is prepared there; a conflicting one is recorded per version and canonical
commit and the batch gets no own-base preparation until it is pre-rebased. Two batches are not
rebased onto `main` this way while `pre_rebase_enabled` is on (decision 153, "Rebase ordering"
below): the next batch, unless the head has failed CI, and the batch behind it in the handoff
window. An unclaimed own-base
preparation on a base other than the canonical commit is neither offered nor kept (`base-behind`). A sealed successor
bypasses both size and quiet gates; its preparation is preferred over other own-base preparations. This happens whether
or not the head has a task of its own, since the long head repairs are when the batch would
otherwise sit idle. `claim_task` offers it after every head task nobody holds, one claim per batch,
within `repair_concurrency` claims in total and at most `repair_concurrency - 1` background claims
(below), so a second worker prepares while the first repairs the head, a head task appearing is
claimed at once by a free worker, and with `repair_concurrency` 1 no preparation is claimed; the claim's `extra.renew_after` is 60 seconds, so a worker learns within a minute
that the controller took the batch back. A preparation is superseded when its batch changes
(`batch-changed`, an insertion or a trim), never by a head task appearing. When the preceding head
lands, an active preparation claim keeps the batch open and renewable until its result is adopted,
the worker releases it or the lease expires. An unclaimed preparation is superseded on sealing
(`sealed`).
While a worker holds the claim, insertion skips the batch and probes the next open batch (created
on the canonical commit when `open_batch_limit` allows), so the preparation never makes inflow wait.
The submission goes through `submit_task` unchanged except that a `prepare` task's batch must still
be `open`: the member commits, provenance and explanations are adopted, the version advances, the
batch stays open and `prepared_version` becomes the new version. A worker whose build is green
releases the task as `prepared_clean`, which records the current version as prepared; a release as
`agent_failed` or `push_failed` resolves the task as `failed` and it is not offered again at that
version. For an unsealed batch, an insertion after preparation advances the version, so the batch
is prepared again (as a whole) once it is quiet. A sealed successor stays closed to insertions. A green background build is never a verdict: the head's CI round, after
the rebase onto the canonical commit of that time, remains the only one.

One background task per batch: `prepare` and `pre_rebase` (`BACKGROUND_TASKS`) are never open
together on a batch, because an adopted preparation rewrites the batch's chain, which supersedes a
pre-rebase and discards its agent's conflict resolutions. A batch preparation applies to
(`prepare_open_batches` on and at least `prepare_min_members` members) is prepared first: while its
`prepare` task is open or claimed, and until it is prepared at its current version (or its
preparation at that version failed), a conflicting replay onto the head's tip is recorded
(`pre_rebase_state` `conflict`) but no `pre_rebase` task is created. Once the preparation is adopted
the next pass replays the prepared chain afresh; once it is released clean the recorded conflict is
replayed again; either way the `pre_rebase` task now created carries, after the conflict and
`cross_batch` entries, a `prepare_errors` entry (`task_id`, `resolution`, `version`, `errors`,
`omitted`) when the preparation left errors: the prepare task's own diagnostics and the
`remaining_errors` list the worker reports in the adopted submission's notes (the errors of its
build, `errors.jsonl` merged with the diagnostics, that no later worker build showed fixed; empty
when the parallel path's verification build passed). The worker's prompt lists them in an "Errors
the batch's preparation left" section beside the predicted ripples. Conversely a `prepare` task is
not created while a worker holds the batch's `pre_rebase` task: the chain must not be rewritten
under the running agent, so the preparation waits until that task is adopted or superseded; an
unclaimed `pre_rebase` task gives way, superseded with resolution `prepare`, and is recreated from
the prepared chain. The pass's `reasons` say which kind waits on a batch and why (`open batch N:
pre_rebase task waits for its preparation (...)`, `open batch N: preparation waits for the claimed
pre_rebase task T`), and each open batch's status carries `background` (below).

Pre-rebase (item 9 of "Pipeline acceleration behind the head" in the plan): once the head's chain is
submitted and validated and waits for its CI verdict (`_pre_rebase_blocked` is None: the head is based
on the canonical commit, has no open task and no claim, and its current version has a dispatched
attempt that is pending, passed, or failed on infrastructure), the oldest open batch is replayed onto
the head's tip with `chain.replay`, never earlier, since the head's unsettled conflicts and build
would leak into the replay; `pre_rebase_enabled` turns it off. The batch stays open on its own base
and keeps its own chain (insertion appends there); the result is recorded beside it
(`pre_rebase_state` `clean`, `adopted` or `conflict`, `pre_rebase_tip`, `pre_rebase_onto` the head's
tip, `pre_rebase_source` the batch tip covered, the members edited) and kept under the clone-only ref
`refs/tauceti-queue/batches/<id>/pre/<onto>`. A later pass carries a usable pre-rebase forward by
replay alone: onto the head's new tip when another repair round or a rebase moved it, and with the
members inserted since appended; when that conflicts the batch's own chain is replayed afresh. A
conflict becomes a `pre_rebase` task on the open batch (`base` and `tip` the batch's, `onto` the
head's tip) with the conflict evidence of a `rebase_conflict` task and a `cross_batch` entry from
`repair_context.cross_batch`: the names a head member removes or renames that a member of this batch
uses (`removed_used`) and the names both batches add (`added_by_both`), text-derived and approximate,
which the worker's prompt lists as predicted ripples. The worker handles it like a rebase conflict,
told that the target is the next head's expected base rather than `main`, but resolves the textual
conflicts only (decision 153, S1): it runs no verification build and so no repair rounds, and saves
no repair checkpoint (`_can_checkpoint_repair` excludes `pre_rebase`). The head's tip it sits on may
itself fail its checks, and a later batch must never act on the head's own failures (task 1416
fixed 465's lint findings through 475's members). The batch is built, checked and repaired once it
is the head, on `main`: by its own CI round and the head's repair tasks, or by the head-time
`rebase_conflict` task's verification. `claim_task` offers it
after the unheld head tasks and before `prepare`, one claim per batch; insertion goes on during the claim, and the submission
is validated against the members the task covers and adopted as the batch's pre-rebase (`adopted`)
with base, tip, version and members unchanged. A task whose `onto` is no longer the head's tip, or
whose batch's chain was rewritten (a preparation adopted), is superseded and its claims released,
except a claimed task whose `onto` is an earlier tip of the current head (a repair round or a rebase
moved it, `_preserve_across_head_round`): resolving a conflict outlasts a head round, so the task is
recorded in `state:emergency_preserved` (`pre-rebase-preserved` in the journal) like the claimed
successor work of an emergency head (below), its owner submits onto that tip, and the next
`_pre_rebase` carries the adopted result onto the head's tip by replay. Landing the head records
the same preservation for claimed successor work made on any of its tips (`landing-preserved`), so a
task stays open when `main` is already past the landed tip when the landing is observed. A
rebuild of the head or a repair that empties a head member discards every pre-rebase. When the head
lands and the canonical commit equals `pre_rebase_onto`, or the pre-rebase carries onto it by a clean
replay (a preserved task that resolved onto an earlier tip of the landed head), the sealed head takes the pre-rebased chain
directly (the `rebased` journal entry carries `pre_rebased`), recording the members edited in the
task as edited; otherwise it is replayed as before, and a `pre-rebase-unused` journal entry says why
the pre-rebase was left unused: `carry-conflict` (it does not replay onto `main`),
`extension-conflict` (members inserted after its task do not replay onto it), `source-not-in-chain`
or `members-mismatch`, with the conflicting member (`source_pr`) and `paths`. The stacked pair is never built by the
controller and nothing about the pre-rebase is a verdict.

When that replay of the head's own chain conflicts, the head-time `rebase_conflict` task starts from
the unused pre-rebase (`_pre_rebased_chain`): while it is usable and its source is in the head's
chain, the task's diagnostics carry `{kind: "pre_rebased_chain", base, tip, source}` (the
pre-rebase's target, its chain and the batch tip it covered), the controller retains the chain as
`refs/tauceti-queue/batches/<id>/pre-rebased/<tip>` (republished with the live batches' refs) and
lists that ref in the claim's retained refs, and the `rebase-conflict` journal entry names the tip
(`pre_rebased`). The worker
(`repair_task.seed_pre_rebased`) records each member that chain resolved as that member's resolution
on the prefix it was made on, so its replay reuses it at once where it meets the same prefix (the
pre-rebase was onto the tip that landed, as for 484 and 490: no helper is started for those
members) and wherever else it is the same conflict (`applicable_resolution`, tried before waiting
for a helper, as for 479). Only the remaining conflicts go to an agent, then the chain is verified
and repaired on `main` as for any head-time rebase. A mechanical resolution the batch's verification
refused is not reused, and the additive pre-pass applies to the remaining conflicts as usual.

Rebase ordering (decision 153): each handoff should rebase the next batch once, onto the head's
tip, rather than onto `main` first and again as the head. While `pre_rebase_enabled` is on:

- R1 (`_successor_rebase_held`): the next batch (the oldest open batch), when behind `main`, gets no
  rebase onto `main` and so no own-base preparation unless the head has failed CI on the source:
  with no head, before the head is dispatched, during the head's own rebase and while its CI runs.
  After a source failure the head's repair may take hours, and a rebase onto `main` is useful then.
  Once the head is dispatched the next batch gets no own-base preparation either, as before.
- R2 (`_handoff_window`): in the handoff window (no head, or a head with no dispatched CI attempt at
  any version) the batch behind the next batch, when behind `main`, gets no rebase onto `main`
  either; the pass's reasons say `open batch N: no rebase onto main until the head is dispatched
  (handoff window)`. It is about to become the next batch. This alone would have prevented the
  rebases onto `main` that blocked 465, 477 and 484 (tasks 1401, 1446 and 1464).
- R3 (`_take_over_rebase`): when the pre-rebase may start (`_pre_rebase_blocked` is None) and the
  next batch has an open `rebase_conflict` task onto `main`, that task is superseded (resolution
  `pre-rebase-first`, journal `rebase-taken-over` with `claimed` and `claim_age_seconds`, counter
  `rebases_taken_over`) if it is unclaimed or its oldest live claim is younger than
  `pre_rebase_takeover_seconds` (1200), whose claim is then released; the pre-rebase starts in the
  same pass. An older claim runs to the end, as before: the batch is then on `main`, and its
  pre-rebase carries only the head's own conflicts.

Preparing the pre-rebased chain (item 16 of "Five improvements from the first production repair" in
the plan), on by default again since decision 174 (`prepare_pre_rebased` true; it was off from
decision 153's S1 until then): a clean replay produces a tip nobody has built, so `_prepare_pre_rebased` creates a
second kind of `prepare` task for the oldest open batch's usable pre-rebase (`clean`, or `adopted`
from a `pre_rebase` task) once it is up to date (on the head's tip, covering the batch's tip),
`_pre_rebase_blocked` allows the head, the head's tip is known-good (below), `prepare_pre_rebased` and `prepare_open_batches` are on, the
batch has `prepare_min_members` members and is prepared on its own base at its current version, no
other background task of the batch is open, and the chain is neither prepared nor already prepared,
released clean or failed for that tip. The task's `base` and `onto` are the head's tip, its `tip` the
pre-rebased tip, and `tasks.pre_rebase_source` the batch tip the chain covers (null for an own-base
preparation); the controller retains the chain as `refs/tauceti-queue/batches/<id>/pre-rebased/<tip>`
on GitHub and the claim's `extra.pre_rebased` carries `source`, the batch's own base (`batch_base`),
the head's id and base (`head_batch_id`, `head_base`), which the worker uses as cache fallbacks. It
is claimed like any background task (one claim per batch, at most `repair_concurrency - 1`), and
insertion skips the batch while it is held. The submission is validated against the members the
chain covers and adopted as the batch's pre-rebase (`pre-rebase-prepared` in the journal): its tip
becomes `pre_rebase_tip`, the members the agent edited join `pre_rebase_edits`, and
`batches.pre_rebase_prepared` records the tip as prepared on that head tip; a `prepared_clean`
release records the task's tip the same way, and `agent_failed` or `push_failed` settles the task as
`failed` for that tip. The batch's own chain, version and members are unchanged. A task whose `onto`
is no longer the head's tip (`head-moved`), or whose chain is no longer a prefix of the batch's
usable pre-rebase (`pre-rebase-changed`), is superseded and its claims released. Sealing waits for an
active owner while the chain remains current; an unclaimed offer is superseded as `sealed`. An own-base
preparation that becomes due waits while a worker holds this task and supersedes it when unclaimed. A head that lands
as the prepared tip's base seals with the prepared tip unchanged, so its CI round fetches the outputs
the worker published for that exact tip; a later insertion extends the pre-rebase by replay, which
leaves it unprepared until the next preparation.

Known-good head tips (decision 174): the next batch is verified and repaired only on a head tip that
has passed its own local verification, so it never acts on the head's own failures (task 1416), and
no failure list passes between batches. `_head_tip_verified` holds when `batches.verified_tip` is the
head's tip and CI has never failed that tip on the source (`_tip_failed`, any attempt of any batch,
so a tip staged again unchanged after a failure stays not known-good). `verified_tip` is set when a task's adopted
submission carries a `local_verification` note (`{tip, checks_passed}`, from the worker) with
`checks_passed` true for exactly the submitted tip (the head's repair or `rebase_conflict`, an own-base
preparation), and when the head seals from its pre-rebase unchanged (`onto` is `main`, no members
replayed onto it) and that pre-rebase's tip is `batches.pre_rebase_verified`: a preparation of the
pre-rebased chain released `prepared_clean`, or adopted with such a note. Any other new tip (a
controller replay, a repair the worker could not verify) is not known-good, and a CI pass alone does
not count: the head lands minutes later, and a preparation started then would only hold up the seal.
Every task offer names `extra.policy_commit`, the canonical commit at the claim, whose `tauceti.toml`
and `scripts/` the worker's checks take their TauCetiTools pin and policy files from (decision 200).
The worker's `attestation` of exactly the tip it verified (a submission's note, or a clean
release's `notes`) is kept beside the verified mark: `batches.attestation` with `verified_tip`, and
`pre_rebase_attestation` with `pre_rebase_verified`, carried to the head when it seals from that
pre-rebase unchanged and discarded with the mark. `_attestation_problem` says why it would not stand
in for staging's checks, or nothing when it would: it must name exactly the tip and its tree, the
sandbox on, a verifier out of candidate code's reach (`verifier_isolated`, which no worker reports
yet, so no attestation counts), the whole-library lint, and a policy commit whose `tauceti.toml` and
`scripts/` are `main`'s now, with the exact tools commit from the repository (and, for a SHA pin,
the commit) `main` pins. Status
shows it as `head.attestation_problem` and `pre_rebase.attestation_problem`.

The attested landing (decision 200, setting `attested_landing_enabled`, default false) acts on it:
a head's dispatch, and the next batch's early run (`_early_staging`), is sent with event
`tauceti-bors-attested` instead of `tauceti-bors-staging` (`ci_attempts.dispatch_kind` `attested`,
`_dispatch_kind`) when the batch's stored attestation of exactly that tip counts, the batch is
GitHub-only, and no attested run of that tip and base has already failed. pr-build then builds
nothing: it requires the observation feed to name the attestation of exactly that commit and tree
(the feed adds `attestation` `{tip, tree, policy_commit}` to a running attested batch), restores every
output with `lake build --no-build`, which fails if any module would need building, runs no audit or
lint, and publishes and posts statuses as staging does (TauCetiTools `bors_attested.py`). An attested
run that does not pass is an infrastructure failure (`attested-fallback: ...`, journal
`attested-landing-fallback`), never a source failure, so the tip is not marked failed; the next
dispatch of that tip and base is full staging. Status shows the attempt's `ci.kind` and `ci.early`.
No preparation starts once the head's CI has passed (`prepare_blocked`: `head N passed CI and is landing`): it would be preserved across the landing and the seal would wait for it with staging idle. On a head that is not known-good, the next batch's conflicts-only pre-rebase still runs, but
`prepare_blocked` says `head N's tip <sha> has not passed its own verification`, and a claimed
preparation is superseded (`head-unverified`) and its submission refused, a preserved one included
once its `onto` has failed. When CI fails the tip a pre-rebase is on, or a repair round or rebase
moves the head past it, `_discard_unverified_pre_rebase` clears an open batch's pre-rebase that
carries a preparation's edits (journal `pre-rebase-discarded` with reason `head-failed`,
`head-moved` or `head-unverified`, counter `pre_rebase_verification_discarded`) instead of carrying
it to the next tip; `_pre_rebase` then replays the batch afresh, and it is prepared again once the
new tip is known-good. One prepared clean, with no edits, keeps its chain and loses only
`pre_rebase_prepared` and `pre_rebase_verified` (`chain_kept` true). A pre-rebase with only a
`pre_rebase` task's conflict resolutions is carried as before. In `claim_task` the next
batch's preparation of its pre-rebased chain ranks with its `pre_rebase`, ahead of every later batch's
own-base preparation, and when the background slots are full it takes the newest such preparation's
slot (`pre-rebase-first`), as the `pre_rebase` does.

Sealing only what was verified (decision 174, step 3; it replaces decision 172's B): the preparation
of the next batch's pre-rebased chain is a frozen snapshot of that batch. Creating the task also seals
the batch's membership (`sealed`, journal `successor-sealed` with reason `verification-snapshot`,
counter `successors_sealed_for_verification`), so insertion sends later arrivals to the following
batch and the run finishes on exactly the members the next head will have. When that preparation
completes and passes, and the head lands as the tip it was made on with no replay needed, the batch
seals from its pre-rebase unchanged, as exactly the verified tip, known-good, and its first staging
run builds a tip a worker already built, checked and published. Otherwise (the preparation is
superseded, fails or goes stale, or an emergency or lane landing forces a replay) promotion is as
before and the head is not known-good; the batch stays sealed at its snapshot. A batch that gets no
such preparation keeps taking members until the usual `sealed_successor_min_members` threshold. The
cost: a batch can be frozen smaller than that threshold (from `prepare_min_members`, 20).

The head's parallel check (decision 174, step 4; decision 172's A): when the head's attempt is
dispatched and pending on a tip that is not known-good and that CI never failed,
`_schedule_parallel_check` creates one speculative `repair` task per head version, whose only
diagnostic is `{kind: parallel_check, proactive: true, attempt_id}` (journal
`parallel-check-started`, counter `parallel_checks`; setting `parallel_check_enabled`, default false, turned on in production as a runtime setting).
It runs alongside the staging run like a predicted repair (`_prediction_task`): it does not hold the
next batch's pre-rebase back, a staging pass supersedes it (`prediction-ci-passed`) and promotes the
head, and a source failure turns it into the head's ordinary repair task with CI's diagnostics (an
unchanged submission is then refused). The worker builds warm and, when the streamed build is clean,
verifies the chain unchanged without an agent; a clean unchanged submission with a passing
`local_verification` records the tip as `verified_tip` (`parallel-check-clean`, journal
`parallel-check-finished`), so the next batch is prepared on it. When the build or the checks fail,
the worker repairs as for any repair task, and the repaired tip becomes the head's next version,
whose dispatch cancels the running staging run (`_cancel_superseded_runs`) and stages the repaired
tip. A released check is superseded (`parallel-check-released`) rather than offered again: staging
remains the authority. A worker without its own warm verification (no audits, cold artifacts) gives
the check back. When `main` advances while a check is claimed, the check is superseded
(`head-rebasing`) so the head is rebased at once, and the rebased tip gets its own check; a
speculative task never re-dispatches a head that is behind `main`.


Early staging (decisions 199 B and 200; `_early_staging`, setting `early_staging_enabled`, default
false): once the oldest open batch is sealed, its preparation on the head's tip has been adopted
with a passing local verification of exactly the chain it seals as (`pre_rebase_verified` equal to
`pre_rebase_tip`, `pre_rebase_source` equal to its tip), and the head is an ordinary batch based
on `main` whose known-good tip is staging or has passed, the controller sends the trusted run of
that prepared tip at once, based on the head's tip: a `ci_attempts` row with `early` 1 at the
batch's current version, `generation` one more than it (the staging candidate branch's name, which
pr-build's train identity needs positive and which the attempt keeps), and `dispatch_kind`
`staging`. It is not held for the base's published cache (the base is the head's tip, published
only at the end of its own run; the run is warm from the preparation's outputs). The observation
feed reports the batch `running` with that tip and base while the attempt is pending, so pr-build's
identity check and the finalizer's revalidation accept it, and stops as soon as it is cancelled.
Each tick observes pending early attempts and cancels one (`_cancel_superseded_runs(only=...)`,
journal `early-staging-cancelled`) once it no longer describes what the batch will seal as
(`_early_invalid`): the batch changed, its pre-rebase changed (for example `_discard_unverified_pre_rebase`
after the head failed), the head's tip moved or failed, `main` moved past the base, the setting was
turned off, or, with one staging slot, the head needs a re-run while the early run is still queued
(it is sent again behind the head's). When the head lands as the base, `_seal_from_pre_rebase`
seals the batch as exactly that tip and adopts the attempt (`early` 2, `batch_version` the new
version, journal `early-staging-adopted`): a pass is promoted in the same pass, a pending run is
the head's run, and a source failure goes straight to the head's repair. Infrastructure failures
of an early run are re-sent `early_staging_infrastructure_retries` (1) times; then the batch is
staged as the head. Verdicts are journaled `early-staging-verdict`; the open batch's status carries
`early_staging`.
CI: the attempt for the head's current version is dispatched with `dispatch_candidate` (the
`repository_dispatch` of `pr-build.yml`, event `tauceti-bors-staging`, payload `head_sha`,
`base_sha`, `batch_id`, `members`, `base_ref`, `repair_outputs_revs`). Observation reads `find_candidate_run` when an event
named the head or `polling_interval_seconds` elapsed, recording runner, host, phases and progress
times. The verdict is the `build`, `bump-guard` and `scope` commit statuses on the tip whose `run_id`
is the run that completed for this attempt, all three `success` (a red `scope` or `bump-guard` beside
a green `build` is a source failure carrying the status description); anything else (no run within `dispatch_timeout_seconds`, a lost run, a cancelled, timed out or skipped run, a completed run without a bound status, a
status from another run, a failure whose description starts with `infrastructure` or whose findings
are all of kind `infrastructure`) is an infrastructure outcome: the candidate is unchanged and is
re-dispatched after `infrastructure_retry_backoff_seconds * 2^min(n,3)`, indefinitely until
recovery or supersession. There is no infrastructure retry exhaustion. A bound successful build
with missing policy statuses keeps its original attempt and uses the existing durable outbox to
replay that run's retained reporting intents. Dispatching recovery is not delivery: the same entry
is rearmed after ten minutes while statuses remain missing. API failures use the outbox's capped
backoff; a controller restart does not lose pending work. Compilation is never repeated for this
reporting-only failure. A failing status with source findings is a source failure and opens a `repair`
task carrying the findings. A pass enqueues the promotion.

Candidate revisions for published outputs: the repair worker publishes Lake build outputs to the
repair cache keyed by the tip it verified (`repair_publish.py`), and `pr-build.yml` fetches them for
the exact `head_sha`. A rebase, rebuild or adoption replaces the tip (new version, new member SHAs),
so that lookup misses although the tree is nearly identical. Because Lake reuses artifacts by
content trace, outputs published for a nearby revision are safe and nearly complete, so every
dispatch carries `repair_outputs_revs`, a JSON list of full SHAs for CI to try in order when nothing
is published for the head: the batch's previous tips from `batch_tips`, newest first and at most
four (`REPAIR_OUTPUTS_PREVIOUS_TIPS`), then the head's `base_sha`, then the canonical commit when it
differs from the base. No SHA appears twice and the head's own tip is never listed, since it is sent
as `head_sha`; a head that was never replayed sends just `[base_sha]`. The list is stored on the
attempt, journalled with `dispatched`, and shown as `ci.repair_outputs_revs` in the head's `/status`
record.

The list also names the published tips of the most recently landed heads. A landed head's tip is in
the repair cache, but the `main` commit it lands as reaches the public cache only once main's own CI
has built it, which takes hours when the landing adds more commits than that CI walks back. The next
head is based on that `main` commit, whose tree equals the landed tip's except for CI files, so
between the head's own previous tips and its `base_sha` the controller appends the `batch_tips` of
the last `REPAIR_OUTPUTS_LANDED_HEADS` (two) batches in state `landed`, newest batch first and each
batch's tips newest first, at most four per batch (`Controller._landed_tips`), without repeats.
Every task's `warm_revs` (`Controller._warm_revs`) names the same tips after the batch tips it lists
and before the base.

Staging never builds on a `main` revision whose Lake cache is not published. Before any dispatch
(after a live run is re-adopted, which needs none) the controller asks the public revision endpoint
(`base_cache_endpoint`) for the map `pr-build.yml`'s cache lookup reads: the URL is TauCetiTools'
`lake_cache_probe.exact_map_url` for the toolchain in `lean-toolchain` at the base. The base is
covered by its own map, or by that of a first-parent ancestor at most `BASE_CACHE_ANCESTORS` (20)
commits back whose tree differs from the base's only outside the Lake build inputs (every `.lean`
file, `lakefile.toml`, `lakefile.lean`, `lake-manifest.json`, `lean-toolchain`; `lake_build_input`),
since the walk of staging's lookup finds that map and every input hash matches. The probe runs
without the controller lock, and the head is re-read after it. A covering revision is remembered
for the process; a miss is remembered for `base_cache_recheck_seconds`. While no map covers the
base the head is not dispatched: the reason line says `head N waiting for base SHA cache to be
published`, the head's status carries `base_cache_wait` (`batch_id`, `base`, `since`, `detail`,
`alerted_at`), `base-cache-wait` is journaled once per base, and after `base_cache_alert_seconds`
the status raises an alert and one direct message is sent (`base-cache-alert`). The dispatch that
follows journals `base-cache-published`. An emergency batch (`batches.urgent == 2`) is exempt, being
how `main` is published again after an out-of-queue push, and journals `dispatch-unpublished-base`.
Source-candidate CI, which reads its own cache scope, and a controller built without
`revision_map_probe` (the fixtures, native sources) skip the requirement.

A run GitHub reports `queued` or `in_progress` is never re-dispatched over. It is lost only when it
can no longer be found (neither in the listing nor by a direct read of the run), or when it is
`in_progress`, nothing in its jobs moved for `lost_run_seconds` (progress is any phase or step
appearing, starting or completing, or the run's `updated_at` moving), and a direct read
(`GET /repos/{repo}/actions/runs/{id}`) still says `in_progress` with an `updated_at` at least that
old; a silence short of that is a `stall:` reason line, never a verdict. A run publishing its final
oleans (`publishing`: the `sandboxed-build` step `Build exact candidate under bwrap` concluded
`success`, and the handoff step or the `publish-merge-group-cache` job has started with no phase of
them concluded unsuccessfully) is never lost, however long it is silent: a publisher that really died
ends when GitHub fails or times out its job, and the completed run is classified as usual. A second dispatch for a tip
whose run is live is never issued, because pr-build.yml's concurrency group would cancel the live run:
before any re-dispatch of a tip dispatched before, the controller asks `find_candidate_run` for the
newest run for it since its first attempt, and when that run is still `queued` or `in_progress` it
re-adopts it whatever the attempt was classified as: the newest attempt row becomes pending again,
bound to the run, and `run-readopted` is journaled. `retry_ci` refuses with reason `live-run` while
such a run exists; cancel the run on GitHub first to force a fresh dispatch.

A run for a candidate the queue no longer uses is cancelled by the controller, not left to the
workflow's concurrency group (which keys on the head and base, so a replayed head's new run never
cancels the old one). After a new attempt is dispatched, and after a batch lands, is removed or has
its head rebuilt, every other attempt of the batch whose run is known and not last observed
`completed` gets `POST /repos/{repo}/actions/runs/{id}/cancel` (`cancel_run` on the port; the App's
`actions: write` covers it). The attempt records `cancelled_at`, the superseding attempt in
`cancelled_by` (empty for a landing, removal or rebuild), `cancel_reason` and GitHub's answer in
`cancel_result` (`cancelled`, `already-completed` for a 409, or the failure), a pending attempt
becomes `superseded` with detail `ci-cancelled: <reason>` (an attempt already classified keeps its
outcome, so infrastructure retries still count), and `ci-cancelled` is journaled with the attempt,
run, `superseded_by` and reason. A failed request (403, 404, 5xx), or a failed lookup of a
superseded attempt's run, is logged and leaves a durable `cancel_intent:<attempt>`, which
`_settle_cancel_intents` retries with the outbox's backoff (`outbox_retry_seconds` doubling up to
`outbox_retry_max_seconds`) until the run is settled; neither holds back the dispatch or the landing.
A `cancel_run` outbox entry left by an earlier controller gets one more try and then hands a failure
to the same intent. A source-CI cancellation (`SourceCandidateCI.cancel_run`) is authorised by the
stored `ci_request_runs` row and its receipt alone, checked against the bound execution repository
and the live run; it never reparses the frozen request or its execution policy, so an attempt frozen
under an older policy schema stays cancellable, and a run with no stored ownership is never
cancelled. A cancelled run is never treated as
lost or as an infrastructure failure: its attempt is no longer observed, the re-adoption of live
runs skips it, and observation of a newer attempt never binds to it.

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

At landing, a final commit whose tree equals its parent's is rejected instead of merged, unless
it is a PR merged externally or an opt-out withdrawal. For an empty commit without a landing note,
the controller also checks the history from the accepted head's merge base to the batch base for
a nonempty commit naming the PR. This uses `chain.landed_prs` and `chain.landed_commit`, with the
same qualified-trailer precedence and GitHub subject rules as chain construction. Git read failures defer landing rather than
classifying unknown history as rejection. A rejected input is
`cancelled` with reason `empty-commit`, with no landing time or SHA. The `landed` journal entry
lists these PRs under `rejected`; `prs_rejected` counts them, merged counters exclude them, and
their per-PR feed outcome is `canceled`. The rejection comment quotes the explanations already
accumulated from the commit message and names the retained staging ref. The existing outbox
removes the membership label and closes the PR with its current title, without the merged prefix;
closure checks the accepted head and skips a fresh approval, and label removal skips a PR that
has joined a new batch.
The existing admission withdrawal rules consume the old approval, so pending closure or reopening
does not automatically requeue the rejected revision; a fresh review or approval can authorize another attempt.
No new agent protocol or repair metadata is required. Intermediate and speculative empty commits
have no rejection effect: the final authoritative commit decides. Empty placeholders remain in
the chain, preserving its one-commit-per-member invariant, and canonical advances ignore them as
evidence of a PR landing.

Provenance and comments: at adoption, `chain.compare` pairs the prior chain with the replacement by
`Source-PR`. A member reported `edited` becomes provenance `edited` with the adopting task, the
appended explanation accumulated onto earlier ones and the commit SHA added to `edited_shas`; a
member reported `clean` keeps its provenance (a mechanical replay after an earlier edit is not a
new edit, and an earlier edit is not forgotten by a later clean replay). The landing comment of an
edited member quotes the accumulated explanations, links the landed commit, shows the diff of the
landed commit against the accepted head in the PR's own files (at most 400 lines, journal
`edit-summary`), links every edited revision, lists the PRs the explanation mentions, and names the
retained ref; a clean member's comment is Bors's notice only. Comment publication is an outbox entry retried independently of the
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
already a member of the open batch, the batch. The open batch seals as soon as the head lands and
its active claims finish; the running head is never cancelled. It never lowers an emergency
(`urgent = 2`) input or batch.

Emergency lane (`emergency_enabled`, off by default): a repository admin's request
(`request_emergency(repo, pr, actor, surface=, reason=, live_head=)`, from `bors emergency`,
`POST /admin/emergency` or `tauceti-queue emergency`) makes an approved PR the next head as a batch
of its own. The flag is never an approval: the PR needs a live input (`waiting`, `reserved` or
`inserted`) whose head is the live PR head, else the request is refused `not-approved` or
`head-moved` (also `disabled`, `forgejo-inert`, `already-requested`); every request and refusal is
journaled (`emergency-requested`, `emergency-refused`). The input gets `urgent = 2` and
`evidence.emergency` (`actor`, `requested_at`, `surface`, `reason`, then the `batch_id` and, for a
displaced PR, `displaced`), and leaves the ordinary draw (`_waiting`). An ordinary insertion never
makes an emergency batch: it copies at most urgency 1 into its batch.

The tick's `_emergency` step (after `_supersede_stale_pre_rebase`, before `_seal`) settles cancel
intents, ends a dead emergency batch E (`empty-allocation`: no member after a crash or a transient
probe failure, retried; `emptied`: no active member; `inert`: a member from a disabled source), and,
when no E is live, builds one for the oldest request: a waiting input is probed (`_probe`) into a
new open batch on `main` with `batches.urgent = 2` (`conflicts-with-main`, `missing-source`,
`missing-source-in-flight` and `source-ci-scope` refuse the request; `probe-error`, `batch-moved` and
`admission-pending` keep it for the next pass); an input inserted in an open batch B is replayed onto
`main` without the lock with the same checks and then, in one transaction, inserted into a new E
whose ownership of the input (`inputs.batch_id`) replaces B's (the prospective singleton; a refusal
leaves it in B). A request whose PR is in the running head waits for that head to land, unless
eviction (below) displaces it. One E is live at a time; later requests queue behind it, first in
first out. E is never evicted. With `emergency_evict` off E waits for the running head to land
(`emergency-waits`).

Eviction (`emergency_evict`, off by default; `_evict`): E takes the head from a running ordinary head
H unless H is protected, decided on H's current attempt in this order (`_protection`): a
promotion-refusal hold is never protection; a pending `promote:<H>:...` outbox entry (P1) or a pass
(P2) is; then, on a fresh strict observation of a pending attempt that may have a run
(`_eviction_observation`: `needs_observation` set, `_observe_attempt(strict=True)`, reloaded; a
source-candidate run in progress also has its phases read afresh, and a failed read means no
eviction this pass), P2 again, a completed run with conclusion `success` whose verdicts are being
bound (P3), and a run publishing its final oleans (P4, `publishing`). Everything else is evicted: a
building or queued run, a publisher still waiting for a runner, a pending or uncertain dispatch, an
infrastructure backoff, a source failure waiting for or under repair, a rebase conflict. An
observation that fails means no eviction this pass; a head found protected by an
observation within `polling_interval_seconds` is not read again until that interval passes. After
the observation everything read is revalidated (same head, version, tip and attempt, no controller
claim on H, still not protected, E still sealable or the prospective singleton still current), then
one transaction: for a PR in H with other active members, E is allocated from the prospective
singleton built beforehand (ownership moves only here); H becomes `open` again, sealed, with its
version and tip, `expected_parent` cleared and `prepared_version = version`; a durable
`cancel_intent` is written for every unsettled attempt of H (a pending one superseded), except an
attempt under a promotion-refusal hold, which H returns to; `state:emergency_evicted:<H>` records
H's version, tip and open tasks; `_make_head(E)`; journal `evicted` (the last observed phases and why
H was not protected). What no transaction prevents: the run can start publishing between the
observation and the cancel.

While H waits, its retained tasks (open at the evicted version) stay submittable and claimable
(`_evicted_task_current`), and nothing else is created for H (`_pre_rebase`, `_prepare_open_batches`,
`_prepare_pre_rebased`); an adoption on H, like a preparation's, records `prepared_version`. Tasks
are offered by the role of their batch, not their kind (`claim_task`, `_choose_task`): the head's
task first, then open batches' work, H's retained repair ahead of pre-rebases and preparations, with
claims on open batches bounded by `repair_concurrency - 1`; while no claim on the head is live, the
head's task is offered even when the claims already number `repair_concurrency`, so live claims can
exceed it by one while H keeps a retained claim. In normal operation role and kind coincide. Once E
is dispatched, H (now the first open batch) is pre-rebased onto E's tip as usual and seals from that
pre-rebase when E lands; the batch behind H keeps its pre-rebase onto H's tip and gets no own-base
preparation while that pre-rebase is usable. If E ends without landing, H is sealed again at once on
its unchanged base. A run with an open cancel intent is never re-adopted (`_readopt_live_run`), so a
returning H dispatches a new attempt. The marker goes when H is head again, lands or is removed.

E is left out of the scheduling selectors only (`_staging_batches`: insertion, `open_batch_limit`,
idle removal, the first open batch for pre-rebase and preparation, successor sealing); retained-ref
republication, sources in flight and status still see it. `_seal` selects `_emergency_ready()` (an
open E with an active member, not inert) before the first open batch and makes it head through the
same checks (`_seal_blocked`: inference hold, external head, claims) and transition (`_make_head`).
As E becomes head, the claimed `pre_rebase` tasks and claimed pre-rebased `prepare` tasks of ordinary
open batches are recorded (`state:emergency_preserved`): while claimed they stay current on their
recorded `onto` and their batch's pre-rebase is not rewritten, so they submit as usual and the next
`_pre_rebase` carries the result onto E's tip. E dispatches on an unpublished base (published base
cache, above) and lands only on a pass of its own run through the normal promotion.

A displaced slot in B never acts for the input (`_displaced`): its withdrawal, anticipation or
return only marks the slot, and B's landing skips it. Every way E's member leaves E goes through
`_emergency_exit` (adoption emptying or anticipation, return, cancellation, and a
rejection when E lands with `withdraw_emptied_at_adoption` off): when B's slot is still live the
input is `inserted` in B again with one comment to the author; otherwise the caller's own behaviour
applies (withdrawal cancels, return waits at the original age). Every way E ends without landing goes
through `_end_emergency`, one transaction that returns its member (or a reservation its own probe
holds), clears the request (except a transient `empty-allocation`), supersedes its tasks, releases
its claims, discards pre-rebases onto its tips, removes it (`emergency-ended` with the cause:
`cancelled`, `dissolved`, `emptied`, `inert`, `empty-allocation`) and writes `cancel_intent:<attempt>`
for every unsettled attempt of E at any version. `_reconcile_head` ends an E emptied between tick
steps before anything can dispatch it. `cancel_emergency(repo, pr, actor, surface=)` refuses
`promotion-pending` once a `promote` entry is pending or the current attempt passed; a promotion
entry for a batch no longer `head` returns `not-head`. `_settle_cancel_intents` (after each such
transaction and first in every `_emergency` pass) looks up the run of an attempt without one whose
dispatch may have been sent, cancels a live run and deletes the intent only once the cancel is
accepted, the run is observed completed, or the attempt provably has no run (`prepared`, `rejected`,
or an `accepted` dispatch with no run found once older than `dispatch_timeout_seconds`; a `sending`
or `uncertain` dispatch is never retired by age). A source-CI lookup that ends in `NoOwner` (a
complete listing with no live, unreadable or owned run) is such an empty lookup, not a failure. A
failure backs the intent off (`tries`, `retry_at`, as for the outbox), measured from the clock after
the failure and never earlier than a rate-limit deadline GitHub gave it; there is no cap, and
intents block nothing. Source-CI cancellations share one GitHub client in deferred mode: they never
sleep or retry in a pass, and the cancel POST's rate-limit headers feed the same counters.

`approve` of a live input's own accepted head by a repository admin (admin evidence with `actor_id`
and `merge_base_sha`) records `evidence.admin_reapproval` beside the original evidence and journals
`admin-reapproved`; the original evidence, `approved_at`, state and batch are untouched. The feeds'
scope approvals read it when the original evidence has no admin scope.

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
task calls) and otherwise every `polling_interval_seconds`. Individual requests obey the shared
client's actual resource deadlines; no fixed quota reserve suppresses a controller pass.

## Settings

Changed at runtime with `set_setting(key, value, actor)`, journaled, effective at the next decision
and reported in status:

| setting | initial | meaning |
| --- | --- | --- |
| `insertion_age_window` | 16 | size of the age window the draw is taken from |
| `open_batch_limit` | 2 | unsealed batches at most; the sealed successor does not consume this capacity |
| `sealed_successor_min_members` | 100 | seal oldest unsealed batches at this size until two active batches are sealed in total, including the head; 0 disables new successor sealing |
| `repair_concurrency` | 1 | maximum live task claims in total; background tasks hold at most one fewer, so 1 allows no background claims |
| `background_start_limit` | 2 | a non-head task may start only while the total live claim count is below this limit; with repair concurrency 3, only a head task can increase the total from two to three |
| `stall_interval_seconds` | 900 | build silence after which status raises a stall |
| `publication_enabled` | false | shadow mode when false: no dispatch, promotion, comment, label, close or ref push; status says what would happen |
| `claim_ttl_seconds` | 900 | claim lifetime between renewals |
| `dispatch_timeout_seconds` | 600 | no run found after a dispatch: infrastructure |
| `lost_run_seconds` | 14400 | an in-progress run whose jobs and `updated_at` have not moved for this long is lost (CI section above) |
| `infrastructure_retry_backoff_seconds` | 60 | base of the re-dispatch backoff |
| `inference_failure_threshold` | 3 | consecutive provider failures that start a hold |
| `inference_hold_backoff_seconds`, `inference_hold_backoff_max_seconds` | 60, 3600 | hold backoff |
| `outbox_retry_seconds`, `outbox_retry_max_seconds` | 30, 900 | outbox retry backoff |
| `polling_interval_seconds` | 60 | polling fallback interval |
| `approval_poll_interval_seconds` | 900 | interval of the approval reconciliation poll behind the webhooks |
| `github_background_floor` | 1000 | queue App requests left at which admission's background reads wait for the window's reset |
| `admission_evidence_mode` | `off` | `shadow` keeps admission evidence from webhooks and compares its verdict with each full read (`admission_evidence.py`); `events` also evaluates from one GraphQL read and ignores status deliveries that change nothing |
| `admission_local_merge_base` | false | merge base from the local clone (complete, unique merge base) instead of the compare API; compare answers otherwise |
| `admission_insertion_check` | false | re-read a GitHub PR's admission inputs before its head joins a batch; hold the insertion and evaluate again unless they match the evaluated digest |
| `membership_label` | `bors-staging` | label added to member PRs at insertion |
| `repaired_label` | `repaired-in-queue` | label added to a landed member whose commit the queue modified (one with an edit summary), created in the repository on first use; empty disables it |
| `insertion_probe_budget_seconds` | 20 | wall-clock budget of one tick's insertion probes; the rest wait for the next tick, which follows at once |
| `outbox_drain_budget_seconds` | 20 | wall-clock budget of one tick's outbox drain; the rest wait for the next tick, which follows at once |
| `prepare_open_batches` | true | create `prepare` tasks for quiet open batches, and pre-rebase such a batch with an agent only once it is prepared |
| `prepare_min_members` | 20 | members an open batch needs before it is prepared |
| `prepare_quiet_seconds` | 600 | time since the last insertion before an open batch is prepared |
| `pre_rebase_enabled` | true | replay the oldest open batch onto the head's tip once the head's chain is submitted |
| `prepare_pre_rebased` | true | create a `prepare` task for the oldest open batch's usable pre-rebase on the head's tip (with `prepare_open_batches`, for the batches it applies to) |
| `parallel_check_enabled` | false | verify a head dispatched on a tip no worker verified on a repair worker alongside its staging run, and repair what fails (`_schedule_parallel_check`) |
| `early_staging_enabled` | false | send the next batch's verified prepared tip to its trusted run while the head is staging, and adopt that run when it seals as that tip (`_early_staging`) |
| `early_staging_infrastructure_retries` | 1 | re-sends of an early run that failed on infrastructure before the batch waits to be staged as the head |
| `attested_landing_enabled` | false | send a tip whose accepted worker attestation counts as an attested landing, which pr-build lands without rebuilding; a failed one falls back to full staging (`_dispatch_kind`) |
| `base_cache_endpoint` | `https://cache.taucetiproject.org/revisions` | public Lake revision endpoint a head's base must be published at before staging is dispatched (CI section above); empty disables the requirement |
| `base_cache_recheck_seconds` | 120 | how long a base found unpublished is remembered before it is probed again |
| `base_cache_alert_seconds` | 7200 | a wait for a published base this long raises a status alert and one direct message; 0 never |
| `emergency_enabled` | false | accept emergency requests (`bors emergency`, `POST /admin/emergency`); off refuses them `disabled`, and a request already accepted keeps running |
| `emergency_evict` | false | the emergency batch takes the head from a running ordinary head unless it is about to land or publishing its final oleans (`_evict`); off, it waits for the running head to land |
| `control_lane_enabled` | false | with mixed sources admitted under GitHub primary, stage GitHub PRs whose own diff leaves the native path policy in the control-file lane batch, sealed next after the emergency batch and never repaired ([mixed-sources.md](mixed-sources.md), "The control-file lane"); off stages a live lane batch as an ordinary one |
| `no_repair_labels` | `["dont-repair-in-queue"]` | labels (case-insensitive) by which an author opts a PR out of agent edits; such a member is only ever emptied and withdrawn as `problem-anticipated` |
| `main_integration_enabled` | false | the main-integration backfill (below): build queued members of unsealed batches on the current canonical commit while PR CI is idle |
| `main_integration_take_out` | true | take a member whose main-integration build failed twice out of its unsealed batch; false only records results |
| `main_integration_max_in_flight` | 8 | outstanding main-integration builds at most |
| `main_integration_interval_seconds` | 120 | time between backfill passes |
| `main_integration_fresh_seconds` | 21600 | a result younger than this is fresh evidence; older ones are asked again |
| `main_integration_timeout_seconds` | 10800 | a build with no result after this long expires and is asked again (three times on the same head) |
| `main_integration_deferred_retry_seconds` | 600 | after CI defers a build for lack of idle capacity, how long the backfill asks for nothing new (then it probes with one build) |
| `main_integration_checks_per_pass` | 20 | static stale checks (`stale_check.py`) a pass runs to order candidates |

## Main-integration backfill

`tauceti_queue/main_integration.py` (decision 175 B), one step of the pass after insertion and
before sealing, off until `main_integration_enabled`. Members of open batches are mostly heads no
CI built on a recent main (Forgejo and self-tested PRs), approved on bases up to a day old. The
backfill asks CI to build them merged onto the current canonical commit, without pushing anything,
through `dispatch_main_integration` (a `repository_dispatch` of TauCeti's `main-integration.yml`;
TauCetiTools `docs/main-integration.md`), and reads the `main-integration` commit status of every
outstanding head in one GraphQL query per pass (`main_integration_statuses`).

- **Candidates:** members of open, unsealed, non-emergency, non-lane batches that are not the head,
  from GitHub (Forgejo members later), whose input has no result younger than
  `main_integration_fresh_seconds`. Order: a red result awaiting confirmation first, then the next
  batch to seal, then members the static check flags (or has not checked), then the oldest base.
  CI runs a build only on idle Fleet PR capacity and otherwise reports it deferred. After a
  deferral nothing new is asked for `main_integration_deferred_retry_seconds`; then one build at a
  time probes until one runs, so a busy pool costs one short picker job per retry interval.
- **Results** count only when the status names the main asked for and was posted after the
  dispatch. Green is fresh evidence on the input (`inputs.main_integration`). A first red result is
  asked again at once; a second takes the member out (below). An error (no verdict, a textual
  conflict, deferral) is recorded and never acted on.
- **Take-out before seal:** only while the batch is still unsealed, open and unclaimed, and only for
  a member no agent has edited. The batch is replayed without the member's commit (the later
  members are cherry-picked onto its parent, a new version), its open tasks are superseded and its
  pre-rebase discarded; the member row is `removed` (`main-integration-failed`), the input cancelled
  with that reason and its approval withdrawn, and the head goes back to its worker or author by a
  head-bound recovery request naming the run (journal `main-integration-removed`). A batch sealed
  meanwhile (decision 174's verification snapshot seals at the start of its preparation), an edited
  member or a replay that conflicts keeps the member (journal `main-integration-kept`); the batch's
  own verification and the head's repair deal with it as before.

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
approved_at=None, pull=None)`, `unapprove`, `cancel`, `set_priority`, `mark_urgent(repo, pr, actor)`,
`request_emergency` and `cancel_emergency`;
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

- `claim_task(worker_identity, github_login) -> Task | None`: an open task under a new claim
  (`claim_token`, `expires_at` = now + `claim_ttl_seconds`) when the login is an admin. Tasks are
  offered within `repair_concurrency` live task claims in total, in two tiers, each by kind
  priority then age: a head `repair` or `rebase_conflict` task nobody holds; then an open batch's
  background task (`pre_rebase` (above), then `prepare` (above)) on a batch where no worker holds
  one, while background claims number fewer than `repair_concurrency - 1` and total live claims
  number fewer than `background_start_limit`. With limits 3 and 2, two background tasks may run;
  an available head task can take the third slot. A head plus one background task already uses
  two slots, so another background task cannot start. Head work has first claim priority, and
  existing work keeps its lease. With `repair_concurrency` 1 nothing runs in the background.
  A task has one live owner. A spare worker waits when no distinct task is claimable; it never
  races another worker on the head. Expiry or release permits a replacement owner.
  A refusal because the total is reached while a head task waits counts in `claim_collisions`.
  The `Task` carries kind, batch and version,
  `base` and `tip` (the chain to start from), `onto` (the commit the replacement must be based on:
  the canonical commit for a rebase conflict, the base itself for a repair or a preparation), members with accepted
  head, current commit, provenance, title, description, author, explanation and retained ref,
  diagnostics, retained refs, `submission_ref` (where to push the result) and the agent instruction.
- `renew_task(task_id, claim_token) -> expires_at`; `release_task(task_id, claim_token, reason,
  error)` (for a `prepare` task, `prepared_clean`, `agent_failed` and `push_failed` settle it, above). Both raise `TaskError` (`unknown-claim`, `claim-expired`, `claim-superseded`) when the
  claim is not live.
- `submit_task(task_id, claim_token, tip_sha, worker_notes) -> Adoption`: fetches the tip if it is
  not in the clone, retains it, then rejects in this order: `claim-expired`, `claim-superseded`,
  `not-authorized`, `version-mismatch` (the task is no longer its batch's current task, or a head
  task's batch is not the head, or a `prepare` task's batch is no longer open), `tip-unavailable`,
  `invalid-chain` (`chain.validate` against the recorded members on `onto`, then `chain.compare`
  against the prior chain: single-parent history, the member bijection by identity, append-only
  messages), `explanation-required` (content changed without an appended explanation; the same
  claim resubmits with it), `no-repair-edited` (a `no_repair` or withdrawn member changed other
  than by reducing it to an empty commit; the worker retries with the violations). Acceptance
  records the new version, base `onto`, tip, member commits and provenance (`withdrawn` for an
  emptied `no_repair` member, above), supersedes other claims and tasks on the batch, retains the tip under
  `.../v<version>`, lifts any inference hold, and the next pass dispatches CI. A rejected chain stays
  retained under `.../submissions/<sha>`.

The worker endpoints answer while a tick runs. The controller lock (`ControllerLock`) is reentrant
with a priority lane: `claim_task`, `renew_task`, `release_task`, `submit_task` and
`report_inference` take it ahead of every ordinary acquirer that is waiting (the tick, webhooks,
operator commands, `status()`). The tick lets go of the lock entirely around its GitHub I/O: each
outbox entry is re-read under the lock, its GitHub call made without it, and its outcome (done, or
failed with the usual backoff) recorded under the lock again, so a drain of thousands of entries (a
landing comment and a close per member of a large head) delays a worker request by one entry at
most; a `promote` entry reads and changes the records and is performed under the lock. Each
insertion probe likewise reserves its input and takes the batch's `insert` claim under the lock,
fetches the head and replays it without the lock, and records the outcome only if the input is
still reserved and the batch neither moved nor was claimed by a `prepare` task meanwhile (else the
input waits for the next pass). The canonical fetch also runs without the lock, and between its
other steps the tick offers the lock to waiting worker requests. Ticks are serialized by a lock of
their own. A renewal's claim expiry is judged at the request's arrival, so a renewal that arrived
in time extends the claim even if it then waited past `expires_at`, and the tick's claim expiry
spares a claim while such a renewal waits. `insertion_probe_budget_seconds` and
`outbox_drain_budget_seconds` still bound how much of each a tick does before the rest of the pass.

## Status

`status()` returns the `tauceti-queue.status/v1` document, JSON-serialisable, with: `repository`,
`canonical_branch`, `canonical_sha`; `service` (running revision, start time, now, last pass);
`settings` (effective values) and `publication_enabled`; `inference_hold`; `waiting` (count, urgent
count, oldest age, max passes waited, the inputs); `open_batch` (the first open batch: id, version,
base, tip, size, oldest member age, members, claim, retained ref); `open_batches` (every open batch
in id order: id, version, base, tip, size, urgent, oldest member age, and the preparation:
`prepared_version`, `prepared`, the open `prepare_task`, `quiet_seconds` since the last insertion;
and `pre_rebase` (null, or `state`, `onto`, `tip`, `source`, `at`, `current`, `edited`, the open
`task_id`, and the preparation of the pre-rebased chain: `preparation` (`preparing`, `prepared` or
`unprepared`), `prepared_tip`, the open `prepare_task_id` and whether it is `prepare_claimed`, and
`prepare_blocked`, why an unprepared chain has no task); and `background`, `active` (null, or the
open background task's `kind`, `task_id`, whether it is `claimed` and, for `prepare`, the `chain` it
prepares, `own_base` or `pre_rebased`) and `waiting` (null, or the other `kind` and the `reason` it waits);
`open_batch` carries the same preparation and `pre_rebase` fields; both forms include `sealed`
and `sealed_at`, and retain the successor for compatibility); `sealed_successor` (null or the first sealed
non-head batch summary; `open_batches` includes every sealed non-head batch); `head` (the same plus `sealed_at`,
`expected_parent`, `based_on_canonical`, `tip_verified` (whether the tip is known-good, `_head_tip_verified`), `stage` in `rebase`, `rebase_conflict`, `dispatch`, `ci`,
`repair`, `infrastructure`, `promote`, the open `task`, `base_cache_wait` (null, or the wait for a
published base described in the CI section), and `ci` with dispatch id, run id and
attempt, status, conclusion, timestamps, runner, labels, host, phases with timestamps, last progress,
outcome and detail, plus `stall` with `since`, `silence_seconds` and the phase when the build has
been silent for `stall_interval_seconds`, and `cancelled_runs`, the batch's attempts whose runs the
controller cancelled: attempt id, version, head SHA, run id and URL, `cancelled_at`,
`superseded_by`, `reason` and `result`); `external_head` (Bors's head awaited after the cutover:
batch, head and base SHA, state, member counts, `pending_members`, timestamps); `approvals`
(`webhook`, `polled`, `revoked_by_poll`, `last_poll`); `anticipated` (the `problem-anticipated`
withdrawals still followed: `number`, `batch_id`, `interacting`, `file`, `finding`, `withdrawn_at`);
`tasks`; `outbox` (pending entries with attempt history);
`reasons` (why the last pass is waiting); `alerts` (the first-line conditions: a stall longer than
two intervals, a hold older than an hour, two chain validation failures on one head, a failing
promotion); `counters` (heads sealed and landed, PRs landed and edited, dispatches and
re-dispatches, passes and source failures, repair rounds, rebase conflicts, shrink-to-empty events,
claim collisions, chain validation failures, inference holds, promotion refusals; for the
repair-or-reject decision: `no_repair_opt_outs`, `edit_summaries` and `prs_edited_landed` (the
repairs), `no_repair_withdrawals`, `problem_anticipated_needs_rebase`,
`problem_anticipated_readmitted`); the recent
`head_cycle_seconds` (first dispatch to landing); `insertion_passes`; `max_passes_waited`;
`emergency` (`enabled`; `batch`, null or the live emergency batch: `batch_id`, `stage` (`waiting` or
`head`), `waiting_for`, and the request's `number`, `state`, `actor`, `requested_at`, `surface`,
`reason`; `queued`, the requests behind it; `cancel_intents`, the open intents with `attempt_id`,
`batch_id`, `reason`, `at`, and after a failure `tries` and `retry_at`). Every batch summary
carries `emergency` (`urgent == 2`) and `lane` (`control` for the control-file lane batch, else
empty); `waiting.control` counts the waiting control-file inputs, and each waiting input carries
`control`.

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
| 11 authorized claim, second claimant and late result cannot compete | F `test_s6_s11_...`, `test_s11_renewal_keeps_a_claim_alive_and_release_frees_the_task`; C one-owner claim, expiry and release regressions (warm artifacts are stream G's) |
| 12 restart recovers outbox, claims and CI state before repeating work | F `test_s12_restart_recovers_pending_outbox_expired_claim_and_repeats_no_work`, `test_s12_loop_runs_passes_on_wake_and_on_the_polling_interval` |
| 13 inference hold | F `test_s13_inference_hold_stops_sealing_keeps_the_head_and_resumes_on_success`, `test_s13_hold_without_a_head_prevents_sealing_until_a_provider_call_succeeds`; C `Holds` |
| 14 shrink to green | F `test_s14_shrink_to_green_rejects_the_empty_contribution_and_names_what_was_dropped` |
| 15 live activation | not expressible locally; the rehearsal (`docs/queue/rehearsal.md`) covers the scratch-repository half of the transition |
| 16 post-admission problem reports do not withdraw | F `test_s16_s21_problem_reports_closures_and_relabelling_do_not_withdraw_a_member` |
| 17 two source failures, no singletons, at most three full runs | F `test_s17_two_source_failures_land_with_three_full_runs_and_no_singletons` (counts only; the 120-minute bound is a pilot measurement) |
| 18 no PR waits more than the window in passes | F `test_s18_no_pr_waits_more_than_the_window_in_passes`; C `Draw` |
| 19 PR events and pushes neither cancel nor verdict; cancelled runs change nothing | F `test_s19_pr_events_and_branch_pushes_never_cancel_a_candidate_or_post_its_verdict`, `test_s10_s19_stale_status_...` |
| 20 urgent lane | F `test_s20_urgent_pr_is_in_the_next_head_without_cancelling_the_running_one` |
| 20b emergency lane | F `EmergencyLane` (every test of the class) |
| 21 closing or relabelling a member changes nothing | F `test_s16_s21_...`; C `test_waiting_inputs_react_to_events_and_members_do_not` |
| 22 status names runner, host, phases, stall, revision | F `test_s22_status_names_runner_host_phase_progress_stall_and_revision`; C `StatusShape` |
| 25 a removal that strands an importer is refused at insertion | F `MissingSource` (`test_s25_removal_that_strands_an_importer_is_refused_at_insertion`, `test_s25_bad_import_is_refused_and_a_batch_mates_module_is_a_source`, `test_s25_gap_already_on_main_and_unavailable_scan_do_not_refuse`); `tests/queue_admission_planning.py` `ImportGaps` |
| 23, 24 increment 0 and transition | not expressible here; `tests/queue_rehearsal.py` runs the cutover rehearsal's scenario script over these fakes (`docs/queue/rehearsal.md`); `tests/queue_bors_import.py` covers the import of Bors's state over a reduced production feed and the first-head rule; `tests/queue_approval_poll.py` the approval poll |

Repair or reject is F `RepairOrReject` (a landing comment with the edit summary and the diff; a
labelled member the agent empties withdrawn with the comment, landing as an empty commit without
being closed as merged; the interacting PR landing converting it to `needs-rebase`; the interacting
PR leaving re-admitting it at its original age, after which it lands with its content; an edit
giving a labelled member content refused as `no-repair-edited`) and C `NoRepair` and `EditSummary`
(the label at approval and from events, the setting, the task flag and an unattributed withdrawal,
the 400-line cap, empty commits naming no landed PR).

Also covered: infrastructure failures retried with backoff and bounded
(`test_infrastructure_failures_are_retried_with_backoff_and_bounded`), shadow mode publishing
nothing and reconciling an external landing (`test_shadow_mode_builds_batches_but_publishes_nothing`). The second open batch (beyond increment 1) is F `OpenBatches`: a conflicting PR is
assembled into a second batch based on the canonical commit, the oldest seals first and the other
keeps receiving, `open_batch_limit` 1 keeps it waiting, a PR conflicting with every open batch
waits, an empty second batch is removed, and a restart attributes a legacy conflict record.
Background preparation is F `Preparation` (a prepared batch seals with its repairs and lands without
a failed CI round where the same batch unprepared fails one; insertion during preparation goes to the
next batch; the head's repair task is claimed first; a second worker prepares the batch while the
first repairs the head) and C `Preparation` (the quiet period and size, the setting, the claim tiers,
a preparation created, claimed and adopted while the head has a task and not superseded by one, a
head task appearing during a preparation claimed at once, no background claim at concurrency 1,
adoption keeping the batch open, sealing preserving active preparations, unclaimed offers and
released or expired claims unblocking sealing, clean and failed releases).
The sequencing of the two background kinds is F `SequencedBackgroundTasks` (prepared, then
pre-rebased from the prepared chain, sealed and landed with both) and C `PreRebase` (no `pre_rebase`
task until prepared, then one from the prepared chain carrying `prepare_errors`; a clean preparation
handing the recorded conflict to a task; a preparation waiting while a `pre_rebase` task is
claimed). The pre-rebase
is F `PreRebase` (made once the head is dispatched, held while it has a task, redone on its new tip,
used by the seal with no replay; a conflict task with cross-batch ripples resolved by the agent and
sealed from; a head tip change superseding an open task) and C `PreRebase` (never before the head's
chain is submitted, never while it has a task, discarded by a rebuild, the setting).

Successor sealing and handoff are exercised against real Git chains in
`tests/queue_sealed_successor.py`: oldest-first selection, the size boundary, immediate preparation,
continued admissions before/after preparation, claim preservation, restart/expiry recovery,
pre-rebase reuse, successor replenishment and fresh authoritative CI after promotion.

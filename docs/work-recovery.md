# Unfinished source recovery

Host checkout preparation force-checks-out `main`, cleans the tree, and later
replaces PR branches. Before any of that, the worker inspects the checkout. When
it holds unpublished commits, staged or unstaged edits, untracked source, or an
unfinished merge/rebase, the worker first writes a recovery record. The same
guard protects the writable roadmap clone used by progress reports, which runs
on the host even for sandboxed workers.

## Containment removal (#364)

Host agents, verification and retained repair use ordinary owned process groups with TERM/KILL
cleanup and existing timeouts. Deliberately detached descendants may survive that best-effort
cleanup. The removed round containment policy, global uncertainty markers and `quiescence.json`
are ignored automatically, even when stale or malformed. Leave historical files and optional
receipt fields in place: no marker deletion, confirmation, manual resume or transcript inspection
is needed to retire them. Source records, recovery journals, publication outboxes, paid counters
and usage history keep their existing formats and integrity checks.

Uncertain destructive-cleanup candidates remain saved or moved aside while unrelated work
continues. No new history scan, GitHub poller, namespace watchdog or cleanup-proof gate is added.
Explicit Bubble sandboxes, claims, budget/fleet admission, verifier continuations and installation
publication locks retain their own existing controls. Shared Mathlib rollout remains off.

[Issue #364](https://github.com/TauCetiProject/TauCetiWorker/issues/364) establishes the baseline for
reduced #338/PR #354 (fast preservation lookup and bounded cleanup), #311/PR #357 (storage
reclamation and rotation), and #310/PR #361 (autonomous recovery). Those changes rebase onto this
removal and target the upstream default branch, without namespace/proof requirements or historical
cwd rebinding. #335/#337 are not planned; PR #352 is closed unmerged. PR #362 is independent.

Local fixture evidence covers stale/malformed markers, unavailable PID namespaces, fresh recovery
checkouts, crash boundaries, original allowances, cancellation, timeouts and accounting supervision.
Implementation and fixture results do not establish deployment or naturally observed recovery.
The existing rollout owner alone deploys at idle boundaries. This change authorizes no production
round termination, capacity/grant increase, synthetic paid work or additional observer.

## Record format

Records live at `state/<worker>/recovery/<round-id>/`, named for the round that
left the source. Each record is private to its worker (mode `0700`/`0600`):

| File | Contents |
|---|---|
| `manifest.json` | schema `tauceti-work-recovery/v1`: repository, checkout, PR, expected remote head, local HEAD, branch, base, work type, roadmap area, source and recording round, creation time, fingerprint, publication state, file sizes and digests |
| `commits.bundle` | unpublished commits; absent when there are none |
| `staged.patch` | binary diff of the index against HEAD |
| `unstaged.patch` | binary diff of the worktree against the index |
| `untracked.tar` | untracked and ignored source with original relative paths, plus the bytes of conflicted and assume-unchanged/skip-worktree paths; symlinks are stored as links and never followed |
| `status.json` | mutable disposition: `preserved`, `checkout-reset`, `retained` or `superseded` |
| `normal-publication.json` | optional durable normal-push intent on reused source; separate from the consumer journal so concurrent journal updates cannot erase it |
| `source-outcomes/` | optional per-handoff checkpoint or clean-inspection outcomes, including publication evidence from the original checkout |

The worker writes into a `.tmp-*` directory and fsyncs every file and the
directory. It then renames the directory into place and fsyncs the parent.
`manifest.json` is written last and lists each payload's size and digest. A
record is initially trusted only when every payload matches; restore always repeats
exact digest validation. Warm preparation may reuse that verification receipt while
the manifest and every payload retain the same device, inode, size, mtime and ctime.
A damaged record is reported as corrupt, kept, and
replaced by a fresh copy. An interrupted save leaves at most a `.tmp-*`
directory, which status reports as incomplete. Nothing in `state/*/recovery`
is ever deleted automatically.

Excluded by name: dependency and build caches (`.lake`, `node_modules`,
`__pycache__`, top-level `build`, `*.olean` and similar) and credential-like
files (`.env*`, `.netrc`, `*.pem`, `*.key`, SSH keys and similar). Being ignored
is not proof of a cache, because `git clean -x` deletes ignored drafts too, so
other ignored files are archived. The manifest lists every excluded path with its
reason, and records conflicted paths deleted from the worktree as `absent`. The untracked archive is
bounded at 128 MiB and 20,000 files; a larger tree fails the save.

## Preparation never waits for a human

OOMs, SIGKILLs and timeouts are routine, so whatever they leave behind must never
stop a worker. Preparation always saves first and then always continues:

- **Clean and published**: no record and no change from earlier behaviour.
- **Anything unpublished** (commits, edits, untracked or ignored source): saved
  in a durable record. A final inspection then checks that the checkout still
  matches it; a write that landed after the snapshot is saved too. The checkout
  is then reset in place. The same source fingerprint reuses its completed descriptor;
  `refs/tauceti/preserved/<record>/...` remain available for compatibility.
- **A process still uses the checkout** (Linux `/proc` cwd/root/open files,
  even when the checkout is clean), saving fails (no space, permissions, the
  archive bound, a git error), the tree keeps changing, or the git state is
  broken (no `.git` after an interrupted clone, a stale `index.lock`, a failed
  forced checkout): the whole checkout is moved aside unmodified to
  `<checkout parent>/retained-checkouts/<name>-<time>-<id>`, a same-directory rename that
  needs no free space, and preparation clones a fresh checkout. Any process
  still working in the old one keeps working there.
- **Retained checkouts clean themselves up in activity housekeeping**. Work-start
  preparation never walks or removes historical trees. The ordinary activity
  collector normally uses a shared 250 ms, 4,096-entry, 16 MiB read and 256 MiB
  removal budget per tick. Low free space raises removal to 1 GiB and 8,192 entries.
  A source candidate that exhausts its allowance gets persisted backoff (60–1,800
  seconds) and a capped allowance on a later tick: at most 30 seconds, 262,144
  entries and 1 GiB of reads. Effective limits are included in prepared timing
  receipts. Cleanup never holds the worker round lock or reserves worker admission.
  Destructive traversal persists its depth-first cursor, including partial
  truncation of uniquely owned large cache files. Directory descriptors are reused
  within a slice, and changed directories are fsynced once per slice. Metadata enumeration resumes
  between collector ticks; a collector restart safely restarts the read-only sweep.
  Filesystem calls themselves are not preemptible.
- **Ordinary ownership and live references protect each candidate**, including cache
  eviction and resumed `*.deleting` trees. The recorded owner must have stopped;
  live users, surviving members of a recorded ordinary round session on the same
  boot, and visible mounts withhold the candidate. Independently started tool
  sessions remain covered by the best-effort cwd/open-file checks; this is no
  guarantee of collecting deliberately detached descendants. Process birth checks do not
  require PID namespace APIs. Repair-cache cleanup takes the consumer's existing
  per-record recovery lock and checks its actual-use generation. No cleanup takes
  the worker round lock. Unknown or unreadable ownership or mount information skips
  that candidate and leaves a retryable next action while other work continues.
  Old containment markers and proofs are ignored. Housekeeping neither creates
  nor maintains them.
  Traversal never follows symlinks or crosses filesystems. A deletion marker alone
  never authorizes destruction. Known untracked top-level caches can be reclaimed
  incrementally; tracked cache-named paths remain source. Before removing source,
  housekeeping saves a descriptor, re-inspects the source, validates the descriptor
  exactly, and journals the deletion before rename. Credentials, native Git
  operation metadata, stash and notes keep the original tree intact. Other reflog-only
  and extra-ref commit tips are included in the existing v1 bundle before the tree
  is removed, including amended-away and force-pushed heads. This history inspection
  happens only in bounded housekeeping; work-start preparation never walks retained
  Git histories. Extra history is capped at 4,096 tips and descriptor metadata at
  4 MiB; exceeding either bound parks the candidate before publishing a descriptor.
  A larger source candidate stays recoverable while its next bounded allowance is
  scheduled. Candidates beyond the maximum allowance keep their source and
  retryable disposition. Unarchived metadata parks only that candidate, keeping
  its last source allowance; subsequent ticks check a fixed set of metadata stamps
  until they change or the daily retry is due. A missing or indirect `.git` never runs
  Git discovery against an enclosing repository; Git commands have a checkout-parent
  ceiling and cache removal requires the expected top-level checkout. No marker removal is needed after failures.

Preparation refuses only when a checkout can be neither saved nor moved aside,
and the next round simply retries. Nothing in this path asks an operator to do
anything; `tauceti recovery` only lists what was saved.

## Preservation lookup

`state/<worker>/recovery-index/<source-key>.json` is a disposable, atomically
replaced, versioned index. Its key includes repository identity and the complete
source fingerprint; its checksum covers the record identity, canonical manifest contents and verified
file metadata. A separate entry for the unpublished commit set reuses its verified
bundle when a clean checkout moves HEAD while retaining the same local branches.
It also binds the descriptor's original source fingerprint, so replacing a manifest
invalidates the artifact receipt. A warm lookup reads one entry, its manifest, and at most five file
metadata stamps. It does not scan other descriptors, hash historical archives, or
walk retained caches. Source inspection still measures the current candidate.

A missing, interrupted, corrupt or incompatible entry is a cache miss. Preparation
saves the current source again rather than trusting the entry or rebuilding history
inline. Background metadata reconciliation can discover existing descriptors, but
registers them as **unverified**: first reuse must verify every payload and establish
an unchanged before/after metadata receipt. Changed or missing payload evidence
likewise requires exact verification or a fresh save. Descriptors, including
incomplete or damaged ones, are never automatically removed. The preservation
format remains `tauceti-work-recovery/v1`.

## Publication

Publication is decided locally and never from GitHub. A commit is published if
it is reachable from a remote-tracking ref, or from `refs/tauceti/published/*`.
`git-safe-push` resolves `HEAD` once, pushes that commit, and records that same
commit after a successful push. The worker writes one
for each PR head it checks out. Nothing in this path pushes, opens a PR, or
infers publication from a transcript.

## Reconstruction

`work_recovery.restore(record, clone)` reconstructs the exact source bytes in a
clone that has the published history. It fetches the bundle, detaches at the
recorded HEAD, applies `staged.patch` to the index and worktree, applies
`unstaged.patch` to the worktree, and extracts the archive. Manually:

```sh
git fetch <record>/commits.bundle 'refs/tauceti-pending/*:refs/tauceti-pending/*'
git checkout --detach <local_head from manifest.json>
git apply --binary --index <record>/staged.patch
git apply --binary <record>/unstaged.patch
tar -xf <record>/untracked.tar
```

## Status

The activity collector adds local counts to the prepared summaries: records,
checkouts moved aside, incomplete and corrupt, with reasons. `tauceti activity`
and `tauceti workers status` show them, and fleet summaries aggregate host counts.
Prepared data includes cleanup backlog,
oldest age and next-action counts, the last preparation's elapsed seconds, Python
source/payload read/hash bytes and Git output bytes, metadata work units, and the
last housekeeping tick's elapsed/read/removal work. These counters measure the
instrumented operations, not kernel-wide I/O. Inventory reconciliation is bounded;
its record counts publish after a completed sweep, while cleanup receipts publish
separately. Failed items add a bounded reason/error count while inventory proceeds;
index publication contention does not freeze prepared counts. Status only reads
prepared JSON and never reads GitHub or inspects trees.

## Rollout and rollback

Rollout does not reset existing checkouts. On a worker's first preparation, its
clean local-only commits reuse their completed bundle; any dirty tree is saved
before the reset, with independent payloads for each changed source snapshot.
Historical `recovery-blocked` dispositions are diagnostic only and never gate later
work starts.

To roll back, set `TAUCETI_WORK_RECOVERY=off` (for example in a worker's `env`
table). This stops index lookup/publication and background housekeeping, including
resumed deletions and historical reconciliation. The existing source-saving guard
stays active: current unpublished source is saved directly in v1 descriptors before
a reset, without consulting the index. Healthy checkouts and their caches remain
in place; an unsavable or live candidate is still moved aside intact. With index
reuse disabled, a clean leftover branch reuses one exactly validated descriptor
named by this checkout's own preserved refs. Only one candidate bundle is validated;
no historical payload inventory is hashed. Bundle prerequisites must still be
published; a rewritten base triggers a fresh self-contained save. Changed dirty source still gets its own
self-contained descriptor. Rollback does not repeatedly clone or retain pristine
checkouts and large private caches.
Existing descriptors, retained trees, deletion journals and indexes remain intact.

## Scope

The guard covers the authoring checkout and the progress roadmap clone, where
work is committed and published. Reference mirrors (`fetch_ref`,
`fetch_git_source`, shared repository attachments) have pushing disabled and
are refreshed as throwaway snapshots; nothing written there can be published.

## Automatic repair recovery

At a new host round boundary, the worker preserves the previous checkout and
attempts bounded recovery before selecting normal work. Mechanical recovery can
run without model funding. Fleet capacity, original authorization, protected PR
labels, claims, current-head CAS and exact verification remain mandatory.

There is no operator recovery queue. The worker either publishes safely,
archives a superseded intent, waits automatically for a dependency or changed
external facts, or retains a blocked checkpoint and continues normal selection.
Final agent output never proves publication or grants permission.

The immutable `tauceti-work-recovery/v1` manifest remains intact. A separate,
fsynced `recovery.json` consumer journal records these states:

| State | Durable meaning |
|---|---|
| retained | Exact source is available for recovery |
| eligible | Recovery may run, or awaits a dependency under a scheduled retry |
| resuming | One claimed invocation owns the isolated checkout |
| verified | An exact-input verifier succeeded |
| publication-pending | Commit, expected head, destination and receipt were saved before push |
| published | Actual remote state confirms the intended commit |
| obsolete | Closed/superseded work or an unsafe-to-replay publication intent stays archived |
| blocked | This checkpoint cannot run under current facts or allowances; normal selection continues |

Only runnable recovery reserves its PR on this worker. A dependency wait gets
a short reservation of at most five minutes; repeated waits never extend it.
Blocked records release their reservation immediately.
Normal work still obeys its own current authorization, holds, claims and existing
repair counters. Before normal owned repair work receives retained source, an
exclusive handoff pauses the old recovery attempt. Its immutable patches and
bundles are evidence in that round, never instructions or publication authority.
A durable normal push consumes the handoff. A refused launch returns it to
automatic recovery; the next owned round also reconciles interrupted handoffs,
prefers retained normal progress, and returns unused source to mechanical
recovery when durable Git intent proves no push was attempted or the attempt
was rejected. This also permits reintegration after another writer moves the head.
The normal saver records the actual captured checkpoint, or a completed clean
inspection, in independent atomic outcome files. The consumer incorporates them
under its existing journal lock; a concurrent source save never rewrites that
journal. These durable outcomes survive disposable-index loss and non-sequential
checkpoint names. They retain scoped publication and uncertain-push evidence
from the original checkout. Before replacement or destructive reset, the live
checkout owner sheds the old handoff attribution; retained source keeps its
original owner record. An interrupted capture
retries through ordinary preservation while independent normal work proceeds;
missing cache entries never prove that no newer source exists. A clean inspection
cannot erase an earlier positive checkpoint receipt.
The original evidence stays available alongside a receiving round's partial progress.
Unpublished edits made after a confirmed push remain evidence for fresh normal
repair work even when the old round's publication authority has ended. Transferred
or archived evidence cannot regain automatic recovery ownership through a new handoff.
Foreign canaries and automatic bumps
do not receive this broader source context. Recovery never replays behind a
normal task that published or a moved head whose publication outcome is
uncertain.
Shared claims and the expected-head CAS coordinate writers across workers.

Recovery restores exact saved bytes into a private clone. For a head that
preserves the original lineage, a clean three-way integration can run
mechanically. After a rewritten head, recovery computes only the delta from the
expected head to the saved source and applies it to a new private checkout of
the current head. It preserves the complete previous checkout and an immutable
source snapshot. A journaled handoff resumes after crashes before, between or
after checkout renames. Removed ancestry is never merged back. Conflicting
deltas receive a narrow, freshly admitted continuation under the original
profile, billing mode, permissions and remaining repair budget. Publication
rejects any continuation that imports the removed lineage, including former
integration targets removed by later rewrites. Local Git refs retain all
lineage inputs across garbage collection and checkout reconstruction.

Paid continuations use the original workflow's content allowance and counters;
there is no additional default one-continuation limit. Failed source verification
can schedule another content repair while that allowance remains. Content
failures are separate from infrastructure failures. Known failures before model
launch refund content counters, while admitted work keeps its charge. A model
that died after resolving a delta gets mechanical verification before another
paid launch. Every paid continuation starts a narrow new provider conversation
in the real private checkout, with retained source and evidence. Recovery does
not rebind a historical cwd or resume a conversation tied to the worker tree.
It requires neither rootless PID namespaces nor containment markers/proofs.
Original sandbox requirements remain in force: this host recovery path cannot
weaken an explicitly selected sandbox. Ordinary owned-process-group cleanup
and configured timeouts remain the runner's responsibility.

Verification uses the installed `tauceti verify` implementation and exact-input
receipts. Committed paths, blob bytes, symlink targets and executable bits are
checked against the verified inventory before CAS publication. Undeclared
scratch files remain archived outside the publication checkout. New source from
an interrupted continuation is checkpointed with source-integrity checks. Full
verification remains required; recovery adds no push-red permission.

A crash after push reconciles actual remote state before replay. A confirmed
push subsequently removed by another writer is archived. A moved head that no
longer contains an unacknowledged pending commit archives that uncertain intent
without replay: its absence cannot prove that publication never happened.
The journal separately records when the push process starts. The safe-push
helper reports permission refusal (77), stale-head rejection (78), server
policy rejection (79), and local preflight infrastructure failure (80)
separately from uncertain transport or remote failure.
Only an explicit destination rejection proves that the push was not accepted;
server policy refusals receive bounded infrastructure backoff. Known-unsent work
can be reintegrated automatically; uncertainty always reconciles first.
The helper retains the earliest unacknowledged normal push within its original
round across subsequent rejections. Annotated Git tags retain source and intent
without introducing synthetic source commits; later rounds have separate intents.
Handoffs use their receiving round's attempt and publication refs, so a later
unrelated push cannot consume them. A supervisor publication still awaiting
reconciliation is offered only as forensic evidence, leaving the supervisor's
scheduled reconciliation active. Known local failures before push clear only
that invocation's never-sent intent.
Source preservation carries this intent into the recovery record or
the separate publication sidecar when identical source was already saved. A receiving
round's checkpoint therefore cannot bypass publication reconciliation. Uncertain
normal publication is confirmed remotely or retained as forensic evidence for
fresh normal work; it cannot authorize automatic replay of a different commit.
An uncertain supervisor push can only retry its exact original commit after
verification. Changed source remains evidence for fresh normal work, and failed
reverification cannot launch paid content repair behind an uncertain intent.
Fresh normal work can assess retained evidence against the current remote head
under its own unchanged policy. No human must edit a journal or resolve a queue.
Recovery creates no extra PRs or comments; existing GitHub intents keep their
normal durable outbox and reconciliation behavior.
An up-to-date push of unchanged source is not a new publication outcome and cannot
consume a retained repair. Exact no-op intents cannot change the remote ref and
do not block mechanical verification of the unpublished edits.

Each phase runs at most one recovery invocation and inspects at most eight
source records. Scheduled metadata checks also reevaluate holds and permissions
without model funding, with a shared thirty-minute scheduling throttle so a blocked
backlog cannot continuously replace normal work. Checks observe PR closure or head changes
through the existing shared state service, without a direct GitHub fallback or
payload hashing. Infrastructure retries back off for
60, 300 and 1800 seconds. Three failed invocations for unchanged facts block that
checkpoint; a scheduled mechanical recheck after the backoff or changed
external facts automatically restores eligibility, even without model funding.
Cumulative failure counts
remain recorded; unchanged repeated infrastructure failures increase backoff
up to six hours. Repeated exact-input verification failures also increase their
mechanical recheck interval up to six hours, even when source and funding have
not changed. Changed external facts reset this delay. Exhausted content allowances permit mechanical reevaluation
and normal work, without authorizing another identical paid attempt.
Claim, capacity, policy, budget and content waits do not consume infrastructure
attempts. Content remains bounded by the original workflow's existing counters.
Consumer protocol 3 automatically re-evaluates older operator-dependent blocks
and obsolete containment waits without resetting paid counters or granting new
permissions. No `operator.json`
file is read or required.

Missing original authorization, unsupported destinations/providers, unsupported
sandbox recovery and v1 native Git operations whose metadata was not retained
cannot acquire inferred replay authority. Their exact source remains blocked as
evidence while fresh normal work continues under current policy. This is a
machine disposition, not a request for operator action. Source, Git history,
immutable records and outboxes are preserved; rebuildable caches are queued for bounded housekeeping from terminal and blocked
checkouts. Recovery integration clones use the same discoverable restoration layout.
A live or uncertain cleanup candidate is skipped without blocking recovery.
Eligible recovery retains its working dependency cache across ordinary waits;
reintegration alone does not enqueue eviction of the live recovery checkout.

`tauceti recovery --json` and prepared activity expose durable state, reason,
next machine action and retry condition. Status does not read GitHub. The
historical PR16035 disposition is retained in
[evidence/issue-310-pr16035-disposition.json](evidence/issue-310-pr16035-disposition.json)
and the main worker state tree's `recovery-dispositions/pr-16035.json`.

Set `TAUCETI_AUTO_RECOVERY=off` to stop starting recovery invocations and release
recovery reservations. Current owned invocations finish. Every checkpoint,
checkout, receipt and outbox is preserved, and the K0 source-preservation guard
remains enabled. Normal workflow permissions, holds and budgets still apply.

The containment framework was removed by
[#366](https://github.com/TauCetiProject/TauCetiWorker/pull/366), tracked by
[#364](https://github.com/TauCetiProject/TauCetiWorker/issues/364). Recovery builds
on that baseline, including its runner, doctor, audit and shared-cache changes. Implementation fixture
evidence does not establish deployment or naturally observed production recovery.

## K3 validation and canary evidence

`tests/recovery_maintenance.py` uses disposable local Git repositories and isolated
state/activity/cache roots. The growing-history fixture adds 32 MiB of descriptors
and 32 GiB of sparse retained caches; warm preparation retains identical read, hash
and metadata counts (44 source hash bytes, 35 metadata work units in the fixture).
Tests also reconstruct saved source after interrupted descriptor publication and
recursive deletion, invalidate corrupt indexes and missing/same-size changed
payloads, resume entry/byte budgets across preparer restarts, preserve unknown/live
owners and mounts, and keep tracked cache-named source and symlink targets intact.
Production-default fixtures preserve 21 MiB of source, revalidate a changed large
payload receipt and reclaim mode-000 overlay directories. Further fixtures show
that unavailable namespace APIs and stale/malformed containment markers cannot
block preparation or unrelated cleanup, a live recovery lock protects its cache,
a new actual-use generation invalidates old cache cleanup, and source, credentials,
Git operation metadata and accounting survive. Old-tree cleanup runs while a new
round holds its admission lock.

These are implementation fixtures, not measured production slowdown or recovery.
No worker deployment or naturally occurring canary sample is recorded here. After
normal rollout, capture prepared preparation and cleanup timing/backlog receipts
from naturally occurring rounds. Do not launch paid work or synthetic CI for a
sample. Issue closure establishes implementation evidence only; capacity and the
$100/hour replenishment grant remain unchanged.

The integration baseline is [PR #366](https://github.com/TauCetiProject/TauCetiWorker/pull/366),
implementing [#364](https://github.com/TauCetiProject/TauCetiWorker/issues/364),
established the containment-free baseline used here. Storage expiry, cache recycling
and log rotation belong to [PR #357](https://github.com/TauCetiProject/TauCetiWorker/pull/357).
This change adds no expiry policy, namespace lifecycle or deployment action.
The retained-checkout roots registered here, `.deleting` trees and
`recovery-cleanup/jobs` journals have one collector owner: this housekeeping path.
The storage collector registers retained roots without enumerating or deleting
those trees; activity housekeeping uses the existing `.recovery-housekeeping.lock`
and journals. Its `save_for_cleanup` helper inspects only the current candidate
and uses indexed descriptors, preserving storage expiry/rotation for other roots.
Pending verification references are checked within housekeeping budgets and
withhold only their candidate. Neither path restores historical preservation scans.

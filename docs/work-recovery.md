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
  Git histories.
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
no historical payload inventory is hashed. Changed dirty source still gets its own
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
runs a bounded recovery phase before choosing unrelated work or selecting a
model. Loop drivers can start a recovery-only child while model funding/quota
is unavailable. Fleet capacity admission still applies. Recovery uses an independent real checkout
and skips a checkout with observed local users (Linux `/proc`; unavailable on other hosts). Rootless PID namespaces, global cleanup markers
and quiescence proofs are not prerequisites.

The immutable `tauceti-work-recovery/v1` manifest and payloads remain intact.
New PR repair owners also record the original GitHub actor, PR author, destination
repository/ref, roadmap, exact provider profile, account restriction, sandbox
permission, and repair allowance keys. Provider session IDs come from structured
provider events and are bound to that identity/profile, never extracted from a
final transcript.

Each record has a separate, private `recovery.json` consumer journal:

| State | Durable meaning |
|---|---|
| retained | Saved source is available; no invocation started |
| eligible | Current policy permits a retry, or a structured dependency wait remains |
| resuming | A claimed invocation owns the isolated checkout |
| verified | An exact-input verifier succeeded; its receipt is retained |
| publication-pending | A particular commit, expected head, destination and receipt are durable before push |
| published | Actual remote Git state contains the intended commit and any publication intents belonging to that outcome are reconciled |
| obsolete | The PR closed/merged, or the exact saved source is already the remote tree; source stays archived |
| blocked | A structured failure names the condition and next action; unrelated work can continue |

The worker restores binary patches, staged/unstaged boundaries, untracked bytes,
and symlinks into an independent clone below the record, without resetting the
normal checkout. An interrupted restoration is retained separately. Verification
runs synchronously through the installed `tauceti verify` implementation; exact
receipts may be reused by its normal input/phase matching. The complete input
fingerprint is checked again before committing. Publication requires the current
writer's head as an ancestor, fresh PR policy, a real claim, and `git-safe-push`'s
expected-head CAS. No recovery comment or new PR is synthesized from agent prose.
Recovery creates no extra comments or PRs. Existing GitHub publication intents use
the durable outbox's normal reconciliation before recovery; unrelated pending
intents do not conceal a confirmed Git push. A crash after push reconciles the actual branch and commit ancestry before any
replay, including after the invocation limit. A durable successful push later
removed by another writer is archived rather than replayed. A moved head with an
unacknowledged pending publication requires explicit reconciliation, because
absence from rewritten history cannot prove that publication never happened.

A clean three-way integration is attempted mechanically and reverified only when
the current head preserves the expected history. Rewritten history is blocked
until only the retained delta has been explicitly re-integrated; removed commits
are never merged back automatically. Owned recovery merges retain their parent
intent across crashes. Leftovers from a round with its own durable successful
push marker are archived without publishing another idea. Changes
already included by another writer, including a remote tree with additional work,
are archived as obsolete. A model runs only for failed source verification or an
integration that needs source reasoning. Unfinished native merge/rebase operations
whose metadata v1 did not retain are blocked until their parents/metadata are
explicitly reconstructed; they never become an accidental single-parent replay. It receives a narrow checkpoint-only
prompt and the original profile/permissions, plus fresh subscription admission that honors pacing even when a normal loop child
received an internal quota override, or fresh API budget admission. A matching durable provider conversation created in this actual recovery checkout is preferred;
a missing or mismatched stable account identity/profile or absent local session
artifact starts a narrow new conversation. OAuth refresh preserves account
identity when its stable account ID is bound to the credential document; separate
cached CLI profiles do not establish that binding, so unknown accounts use a
narrow new conversation. Conversations from historical working directories also start narrowly
in the fresh checkout, without binding or modifying the historical path. The GitHub proxy, safe-push helper and pre-push hook reject
continuation publication; its origin is fetch-only and GitHub token environment
variables are removed. The supervisor verifies the resulting source before
publication and checks committed blob bytes, paths and executable bits against
the verified inventory. Recovery commits disable hooks. Changed
source is retained using the same checkpoint format, including new files left by
an interrupted continuation once no local checkout users are observed. Content repair allowances
remain separate from infrastructure retries; known failures before model launch
refund content allowances. Publication includes only tracked changes and declared
untracked source covered by the verifier. Ignored drafts outside that inventory,
provider settings, notes and logs stay archived outside the publication checkout;
continuation diagnostics are written below the recovery record, outside source. Full verification is required;
recovery does not introduce a new source-only push-red route.

One invocation and at most eight source inspections are allowed per phase, with
a cursor to avoid starvation. Archived and unchanged blocked metadata do not
consume those source slots. At most one blocked PR receives a cheap current-fact
probe per phase, at 30-minute intervals, to observe closure or changed heads. Infrastructure failures retry after 60, 300 and
1800 seconds; three failed invocations for unchanged checkpoint/external facts
block further invocations. Claim, policy and admission
waits do not consume those attempts. Changed PR facts re-enable eligibility.
Selection-local waits do not schedule recovery-only children until the original
operator policy changes. The loop preserves its requested provider policy
separately from its model selection for unrelated work. Failed exact-input
receipts may avoid repeating failed builds while waiting for continuation
admission; they never authorize publication.
Quota-independent probes run at most once per five minutes; a probe that made no
progress returns no-progress, preserving normal work and backoff behavior.
An operator can place `{"retry_epoch": 1}` in a record's `operator.json` after
repairing infrastructure; changing the epoch re-enables the bounded retries.
`content_allowance` can explicitly authorize another narrow continuation without
resetting the original workflow's repair counters. Legacy records lacking durable
publication authorization, sandbox-required rounds without a supported isolated
sandbox recovery backend, unsupported destinations, new-PR authoring without a
structured PR target, and exact one-launch foreign canaries retain a structured
blocked disposition rather than gaining inferred permission. A trusted operator
may supply the original structured `authorization` and an explicitly identified
original `pr` in `operator.json`. After reconstructing and finishing a native
operation in `record/checkout`, `operation_repaired: true` allows exact verification
and current-head integration under the same publication checks.

`tauceti recovery --json` and prepared local activity expose the consumer's durable
state/reason; status does not read GitHub. The fresh-repair fence is worker-local;
shared claims and the expected-head CAS coordinate writers across the fleet. For the historical PR16035 case, see
[evidence/issue-310-pr16035-disposition.json](evidence/issue-310-pr16035-disposition.json).
Terminal and blocked recovery queue bounded housekeeping for rebuildable caches in isolated/restoration clones;
source, Git history, immutable records and outboxes remain archived.

The production disposition is also retained under the main worker state tree's
`recovery-dispositions/pr-16035.json`; its search scope is explicitly local.

Set `TAUCETI_AUTO_RECOVERY=off` to stop starting recovery invocations. An invocation
already owning work finishes; its checkout, checkpoint, receipt and outbox are
preserved. This switch does not disable the K0 source-preservation guard.

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
Storage collectors reuse registration/housekeeping and the existing
`.recovery-housekeeping.lock`, rather than independently recursively deleting those
same trees or restoring historical preservation scans.

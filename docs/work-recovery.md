# Unfinished source recovery

Host checkout preparation force-checks-out `main`, cleans the tree, and later
replaces PR branches. Before any of that, the worker inspects the checkout. When
it holds unpublished commits, staged or unstaged edits, untracked source, or an
unfinished merge/rebase, the worker first writes a recovery record. The same
guard protects the writable roadmap clone used by progress reports, which runs
on the host even for sandboxed workers.

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
record is trusted (for reuse, for exempting its commits, or for restore) only
when every payload matches; a damaged record is reported as corrupt, kept, and
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
  is then reset in place. Commits are bundled once and marked
  `refs/tauceti/preserved/<record>/...`.
- **A process still uses the checkout** (Linux `/proc` cwd/root/open files,
  even when the checkout is clean), saving fails (no space, permissions, the
  archive bound, a git error), the tree keeps changing, or the git state is
  broken (no `.git` after an interrupted clone, a stale `index.lock`, a failed
  forced checkout): the whole checkout is moved aside unmodified to
  `<checkout parent>/retained-checkouts/<name>-<time>-<id>`, a same-directory rename that
  needs no free space, and preparation clones a fresh checkout. Any process
  still working in the old one keeps working there.
- **Retained checkouts clean themselves up**: each later preparation handles up
  to three of them. A copy nothing uses any more is saved into a record,
  re-inspected to confirm nothing changed, renamed to `*.deleting` and removed;
  an interrupted removal is finished later. A copy that cannot be saved keeps
  its source but has its dependency and build caches evicted, so retained
  checkouts do not fill the disk with build output.

Preparation refuses only when a checkout can be neither saved nor moved aside,
and the next round simply retries. Nothing in this path asks an operator to do
anything; `tauceti recovery` only lists what was saved.

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
and `tauceti workers status` show them, and fleet summaries only add host counts.
Status never reads GitHub.

## Rollout and rollback

Rollout does not reset existing checkouts. On a worker's first preparation, its
local-only commits are bundled once and any dirty tree is saved before the
reset. Records left `recovery-blocked` by the first version of this guard are
marked `superseded` when their checkout is next prepared.

To roll back, set `TAUCETI_WORK_RECOVERY=off` (for example in a worker's `env`
table). Preparation then writes no records and skips inspection. A clean
checkout is reused as before, but one that `git status` reports dirty is still
moved aside rather than reset. Existing records are never deleted.

## Scope

The guard covers the authoring checkout and the progress roadmap clone, where
work is committed and published. Reference mirrors (`fetch_ref`,
`fetch_git_source`, shared repository attachments) have pushing disabled and
are refreshed as throwaway snapshots; nothing written there can be published.

## Automatic repair recovery

At a new host round boundary, the worker preserves the previous checkout and
runs a bounded recovery phase before choosing unrelated work or selecting a
model. Loop drivers can start a recovery-only child while model funding/quota
is unavailable. Fleet capacity admission still applies. Recovery requires strong
PID containment and no unconfirmed cleanup. It never restarts a live session.

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
received an internal quota override, or fresh API budget admission. A matching durable provider conversation is preferred;
a missing or mismatched stable account identity/profile or absent local session
artifact starts a narrow new conversation. OAuth refresh preserves account
identity when its stable account ID is bound to the credential document; separate
cached CLI profiles do not establish that binding, so unknown accounts use a
narrow new conversation. During a resume, the original cwd is bound to the isolated checkout in
the same containing namespace so historical absolute paths cannot edit the live
worker checkout. The GitHub proxy, safe-push helper and pre-push hook reject
continuation publication; its origin is fetch-only and GitHub token environment
variables are removed. The supervisor verifies the resulting source before
publication and checks committed blob bytes, paths and executable bits against
the verified inventory. Recovery commits disable hooks. Changed
source is retained using the same checkpoint format, including new files left by
an interrupted continuation once containment confirms quiescence. Content repair allowances
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
block further invocations. Claim, verification-capacity, policy and admission
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
Terminal and blocked recovery evict rebuildable caches from its isolated/restoration clones;
source, Git history, immutable records and outboxes remain archived.

The production disposition is also retained under the main worker state tree's
`recovery-dispositions/pr-16035.json`; its search scope is explicitly local.

Set `TAUCETI_AUTO_RECOVERY=off` to stop starting recovery invocations. An invocation
already owning work finishes; its checkout, checkpoint, receipt and outbox are
preserved. This switch does not disable the K0 source-preservation guard.

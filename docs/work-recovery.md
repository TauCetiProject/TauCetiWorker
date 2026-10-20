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

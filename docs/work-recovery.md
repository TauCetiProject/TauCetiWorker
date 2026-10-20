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
| `untracked.tar` | untracked source with original relative paths, plus conflicted paths; symlinks are stored as links and never followed |
| `status.json` | mutable disposition: `preserved`, `recovery-blocked`, `checkout-reset` or `superseded` |

The worker writes into a `.tmp-*` directory and fsyncs every file and the
directory. It then renames the directory into place and fsyncs the parent.
`manifest.json` is written last, so a directory without a valid manifest is
never treated as a record. An interrupted save leaves at most a `.tmp-*`
directory, which status reports as incomplete. Nothing in `state/*/recovery`
is ever deleted automatically.

Excluded by name: dependency and build caches (`.lake`, `node_modules`,
`__pycache__` and similar) and credential-like files (`.env*`, `.netrc`,
`*.pem`, `*.key`, SSH keys and similar). Ignored files are build output. The
manifest lists every excluded path with its reason. The untracked archive is
bounded at 128 MiB and 20,000 files; a larger tree fails the save.

## When a reset may proceed

- **Clean and published**: no record and no change from earlier behaviour.
- **Commits only**: the commits are bundled once and marked
  `refs/tauceti/preserved/<record>/...`, and preparation proceeds. Commits are
  immutable objects, so a late writer cannot change what was saved.
- **Working-tree source**: the record must be durable *and* the checkout
  quiescent. Until round containment reports an empty boundary for the round
  that left the source, quiescence is unknown. The checkout is then retained,
  marked `recovery-blocked`, and the round fails with the record path and the
  command to run.
- **Any process using the checkout** (Linux `/proc` cwd/root/open files) blocks
  the reset, even after an operator confirmation.
- **Saving or inspection fails** (no space, permissions, archive bound, a git
  error): the checkout is left intact and the refusal says why.

To release a blocked checkout, first confirm that nothing from the earlier round
still runs there. Then run:

```sh
tauceti recovery                                  # list local records
tauceti recovery confirm <record> --worker-id <worker>
```

A confirmation applies only to the exact saved source, compared by fingerprint.
If the checkout changes after confirmation, the next preparation saves a new
record and blocks again. A round-containment owner may instead write
`state/<worker>/quiescence.json` as
`{"round_id": ..., "empty": true, "boundary": ...}` for the exact round recorded
in `state/<worker>/checkout-owners/TauCeti.json`.

## Publication

Publication is decided locally and never from GitHub. A commit is published if
it is reachable from a remote-tracking ref, or from `refs/tauceti/published/*`.
`git-safe-push` writes that ref after a successful push. The worker writes one
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
blocked, incomplete and corrupt, with block reasons. `tauceti activity` and
`tauceti workers status` show them, and fleet summaries only add host counts.
Status never reads GitHub.

## Rollout and rollback

Rollout does not reset existing checkouts. On a worker's first preparation, its
local-only commits are bundled once. A dirty existing checkout is saved and
blocked like any other.

To roll back, set `TAUCETI_WORK_RECOVERY=off` (for example in a worker's `env`
table). Preparation then writes no new records, but it still refuses to reset a
checkout that has a `recovery-blocked` record. Never delete retained records.

# Experimental rebase driver

`tauceti_queue.rebase_driver` is an opt-in local prototype. The production task runner does
not import it. It separates Git replay from conflict resolution: the driver merges each
original member onto the new prefix, commits clean members itself, and stops at every real
conflict. There are no automatic Lean, import, or documentation conflict resolvers.

The resolver edits files and supplies an explanation. It does not run `git rebase`, commit,
amend, stage, or autosquash. The driver creates exactly one commit per original PR, including
empty commits, in the original order. It preserves the original author, author date, message,
`Source-PR`, and `Accepted-Head`, appending the resolution explanation immediately.

## Local use

Use Git with `merge-tree --write-tree --merge-base` support and Python 3.11 or later. Make a
standalone scratch clone, remove its remote, and put driver state outside the checkout:

```sh
git clone --shared --no-checkout /path/to/local-object-store /tmp/rebase-test/repo
git -C /tmp/rebase-test/repo remote remove origin
git -C /tmp/rebase-test/repo checkout --detach TARGET_SHA
uv run python -m tauceti_queue.rebase_driver --state-dir /tmp/rebase-test/state init \
  --repo /tmp/rebase-test/repo --base ORIGINAL_BASE_SHA --tip ORIGINAL_TIP_SHA --target TARGET_SHA
```

The command returns JSON naming the current conflict packet, or reports completion. The
packet contains the source patch (up to 64,000 characters, with a truncation flag), source PR
and accepted head, prefix and target SHAs, original member paths, conflict paths, recent target
history on those paths, and exact base/ours/theirs blobs. Stages 1/2/3 mean base/ours/theirs.
Some conflict types have only two stages. The source commit remains available for larger patches.

Edit the conflicted files in the scratch checkout and write a concise explanation to a file:

```sh
/tmp/rebase-test/repo/.tauceti-repair/resolve \
  --conflict-id ID_FROM_PACKET --explanation-file /tmp/resolution.txt
/tmp/rebase-test/repo/.tauceti-repair/status
/tmp/rebase-test/repo/.tauceti-repair/check
```

`resolve` accepts the edits, creates the attributed commit, and advances to the next conflict.
Edits must belong to the original member or its conflict paths; an additional dependent Lean
file requires `--allow-path TauCeti/path/to/File.lean` and is recorded on that same PR. This
does not support repairs belonging to a different, earlier PR: those require a separate repair
and folding step. Original infrastructure PRs can include infrastructure paths, but additional
paths are restricted to TauCeti Lean sources. The driver does not execute the repaired code.

`status`, `advance`, and `check` can be called after restarting Python. The immutable plan
binds source members, source and target SHAs, and policy. A private ref and atomic journal
recover interrupted commit publication and checkout materialization. A conflict ID binds the
resolution to its source member and current prefix; stale IDs fail and duplicate submissions
return the already accepted result. External HEAD/ref changes, unexplained edits, remaining
conflict markers, and symlinked resolution paths fail. The resolver's Git index is ignored;
a separate driver-owned index supplies both the expected tree and cached file metadata.

This is an accidental-error boundary, not a sandbox against a hostile resolver with access to
the checkpoint directory. A shared scratch clone borrows objects from the local source store;
avoid source-store garbage collection during a replay. No production worktree is registered.

## Replay an adopted job

```sh
uv run python -m tauceti_queue.rebase_replay \
  --source /path/to/local-object-store \
  --checkpoint /path/to/tasks/88/task.json \
  --reference ADOPTED_RESULT_SHA \
  --destination /tmp/replay-task-88
```

This creates a new scratch clone without remotes, freezes the original task's source and target,
and feeds the adopted result's resolutions through the driver. It never claims or submits a
queue task and makes no GitHub requests, pushes, inference calls, or Lean builds. The production
checkpoint is read locally; its credentials and claim tokens are not copied into the replay.
The report and packets remain for inspection. The scratch checkout is removed on success unless
`--keep-repo` is passed; on failure it remains for debugging. Remove failed scratch directories
when finished. Retained checkpoints need their scratch clone to resume.

The harness uses a special `review_clean` mode to inspect every member, even a clean merge.
This matters because the adopted chain may contain build fixes folded back into earlier PRs.
It compares the current prefix with the reference parent, applies only that member's reference
tree differences, and checks the resulting tree before advancing. Clean candidates that differ
from the adopted member are reported as recorded corrections, separately from Git conflicts.
It checks every member tree, final tree, membership, order, accepted heads, original authorship,
and append-only messages, and restarts the driver after every 25 members.

An exact historical replay proves driver bookkeeping and compatibility with recorded repairs.
Its elapsed time measures the oracle harness, including clean-member inspection. It does not
measure fresh-agent speed or resolution quality. Adoption of the reference is not evidence of a
passing Lean build. A later benchmark should run a fresh resolver against these same frozen
inputs, with identical resource limits and the usual build and policy audits before adoption.

The first [recorded replay](rebase-driver-task88.json) used adopted task 88 (batch 391):
349 members, 37 Git conflicts, 298 clean matches, and 14 clean members with recorded corrections.
Every member tree and the final tree matched the reference; attribution checks passed. The
driver was reloaded from its checkpoint 13 times. Replay and Git checks took 272.802 seconds,
excluding clone setup, with no agent inference or Lean build. The scratch checkout was removed.

## Remaining integration

Worker claim renewal, agent invocation, model fallback, build feedback, multi-PR fixup folding,
the full repair policy/audit suite, and submission remain in the existing worker. The prototype
has no production feature switch yet. Its `check` covers Git/attribution invariants only; passing
it must not be treated as permission to submit or as proof that the mathematics is correct.
Unusual non-file merge conflicts without unmerged blob stages stop for manual inspection.

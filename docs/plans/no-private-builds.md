# No private copies of Mathlib, TauCeti or their builds

A worker host keeps a fixed, small number of copies of the things it builds
against, and every round reaches them by reference. Worker code never makes a
private copy: a failure to share stops the round instead of falling back to a
copy. What an agent can do with arbitrary code is bounded by a hard per-round
disk quota, so a copy fails rather than filling the disk.

This plan overturns earlier decisions: private fallback for shared Mathlib
([shared workspace dependencies](shared-workspace-dependencies.md),
[cache preparation](cache-preparation-efficiency.md)), the warm private workspace
per worker, and whole-tree retention in recovery
([storage lifetimes](storage-lifetimes.md), [retained build trees](retained-build-trees.md)).
Where those plans require private copies, this one supersedes them.

## Evidence

On 14 November 2026 tauchonki (3.5 TiB) filled to 100% several times within an
hour, with a load average of 200 to 740. Measured there:

- **Every worker checkout held its own unpacked Mathlib build**: 9.6 GiB under
  `.lake/packages/mathlib/.lake`, with a link count of 1 on its `.olean` files. It
  also held its own TauCeti build: 5.4 GiB under `.lake/build`. There were 93
  checkouts, so these copies alone came to about 1.4 TiB.
- **Shared Mathlib was enabled but mostly unused.** The policy was `auto`. Of the
  60 most recent per-worker decisions, 40 were `private` with the reason "sharing
  was skipped because the Mathlib checkout was absent", and 13 were `shared`.
  `shared_mathlib.view()` runs before `.lake/packages/mathlib` exists in a fresh
  checkout, so it chooses private, and `lake exe cache get` unpacks a full copy.
- **Writes reached about 36 GiB in 30 seconds**, from `/proc/<pid>/io` across all
  processes, mostly worker loops and their reaped children unpacking archives at
  round start.
- **Recovery and discard kept whole trees.** Three workers' `recovery-cleanup/discarded`
  held 83 GiB, mostly `checkout/.lake`. On chungus2 the same queues reached about
  420 GiB, with single entries up to 64 GiB.
- **Deletion stopped.** A `tauceti-cleanup.service` run from 13 November 05:25 UTC
  was still active 26 hours later, in kernel I/O wait, holding both `cleanup.lock`
  and `.recovery-housekeeping.lock`. Every in-process sweep skipped, and the hourly
  timer could not start another run.
- **Some trash could not be deleted:** overlayfs work directories
  (`tauceti-overlay-work/work`) left with mode `000`.

## Enforcement boundary

"Impossible" has two parts, because an agent runs arbitrary code with read access
to Mathlib and write access to its workspace, and no permission scheme stops it
running `cp -r` on readable files:

1. **No worker code path copies.** Every place the worker restores, links, unpacks,
   snapshots or clones build inputs either references shared state or fails the
   round. There is no fallback to copying, including when a hard link fails.
2. **A hard per-round disk quota.** Each round's writable space (its worktree and
   scratch) lives under a filesystem project quota or a size-capped mount. A copy
   by any means fails when it reaches the quota, and the round is killed and
   abandoned. The quota is the guarantee; detection only explains what happened.

Shared state is owned by trusted publication code. Rounds receive read-only views
of it. Its canonical paths are protected by ownership or mounts, not only by mode
bits a cooperative caller would respect.

## Invariants

1. **At most two Mathlib versions per host, ever.** A version is its source tree,
   its extracted build and its archives. The host holds the version current
   TauCeti `main` pins and the one before it, never a third, not even briefly.
2. **TauCeti outputs exist once.** Each distinct build output is stored once in a
   host artifact store; checkouts reach it by hard link. A completed build's
   outputs are replaced by links to the canonical file.
3. **TauCeti versions are bounded by running rounds.** Long-lived TauCeti state is
   one object store and two reference builds, for current `main` and the previous
   `main`. Every other worktree and branch-specific output exists only while a
   round uses it.
4. **Recovery saves source, never build state.** Recovery records hold a commit
   bundle, patches and bounded untracked files, and never `.lake`. Build state is
   dropped the moment a tree is moved aside. A failed save never authorizes
   deleting source.
5. **Deletion keeps making progress.** No single stuck process can stop deletion
   on a host.

## Steps

Each step ships as its own PR, deploys live without draining, and is checked on
real hosts before the next starts.

### 1. Collector progress

- `tauceti-cleanup.service` gets `TimeoutStartSec`, and the timer always runs the
  collector with `--max-seconds`.
- Deletion stops depending on one broad lock. Under a short lock the collector
  moves eligible trees into a uniquely named, permanently owned trash generation
  and records it. Trash generations are deleted outside the housekeeping lock, so
  a generation stuck in kernel I/O blocks only itself. Locks are never stolen or
  recreated: a stalled holder may resume at any time, so it is isolated instead.
- Before removing a directory, deletion restores owner write and execute
  permission on it (keeping the existing no-follow and mount checks), including
  overlayfs work directories left with mode `000`.
- The in-process sweep and the fallback collector cover every root the timer
  would; a stale `cleanup-service.json` never suppresses deletion.

### 2. No build state in recovery

- When `_retain`, abandonment or `discard_record` moves a tree aside, its `.lake`
  (and other known build directories) goes straight to a trash generation.
  Source stays protected as today.
- Recovery records stop materializing `checkout/` with build state. A record that
  needs to resume work restores its source into a fresh worktree later.
- Failure cases stay explicit. A failed save, damaged Git state or a still-running
  writer parks the source as today; only build state is dropped.

### 3. Shared Mathlib that actually happens

- **Ordering.** Resolve the Mathlib package from `lake-manifest.json` and attach
  the shared source tree before `view()` decides, so a manifest that names Mathlib
  never yields "checkout absent".
- **Layout.** Specify exact paths: an immutable source tree per revision, an
  immutable build per key selected through a per-consumer view, and private
  writable metadata. Relax `_build_parent_owned()` only for that layout. Prove two
  build keys against one source revision before rollout.
- **Identity.** Narrow `signature()` to the inputs that change Mathlib's build
  (revision, toolchain, ABI, platform, options), not TauCeti configuration or
  unrelated dependencies. Publishers use the canonical source tree instead of
  cloning one per entry.
- **Required.** Remove the `off` and `auto` modes and the private branch of
  `view()`. A host that cannot share runs no rounds that need Mathlib, and says why.
- **Two-version cap.** Publishing a third version first evicts the oldest. Rounds
  holding a lease on it are killed and abandoned without recovery, then it is
  deleted, then publication starts. A round pinned to any other version is refused
  and its task dropped; branches behind `main` are rebased onto current `main` or
  abandoned.
- **Dependency changes.** Lake can fetch and check out packages while loading,
  and agents are told to fetch Mathlib after rebasing. Dependency resolution goes
  through the trusted publisher, which resolves and leases the new graph before
  switching the view. Prompts and helpers change with it.
- **Existing entries.** Do not chmod entries under readers: `inventory()` records
  mode and link count, so a change invalidates `ready.json` and can trigger a
  pool-wide safety stop. New, versioned entries carry the read-only layout.
- **Rollout.** Prewarm the actual keys, then require sharing on one host first.

### 4. TauCeti outputs exist once

Stock Lake compiles into `.lake/build` and caches afterwards. If the canonical
artifact already exists it leaves the local file in place, so concurrent builds
leave independent copies. And `restoreArtifact` (like `expanded_cache.link()`)
copies when a hard link fails.

- **Canonical relinking.** After each build step, worker code replaces completed
  outputs with hard links to the canonical artifact, publishing new ones
  atomically. Identical concurrent builds converge on one inode.
- **Link failure is fatal** in the worker's restore and hydrate paths. Expansion
  must complete for the selected inputs before Lake runs; partial expansion is a
  failed preparation, not a fallback.
- **Metadata stays separate.** Payloads are immutable and shared. Output mappings
  are written atomically and stay private, keeping trusted CI mappings distinct
  from mappings produced locally. Executables keep their execute bits.
- **Reference builds.** Two read-only worktrees track current `main` and the
  previous `main`; their links keep `main`'s outputs alive. When `main` advances,
  the older reference is dropped.
- **Branch deltas.** A round's own outputs are referenced only by that round, and
  are released when its worktree is removed.
- **Per-round build cap.** A round producing more new outputs than a limit (for
  example 6 GiB) is killed and abandoned. With 30 concurrent rounds and a 5.4 GiB
  full build, the ceiling is about 11 + 30 × 5.4 ≈ 170 GiB, against about 500 GiB
  of private TauCeti builds on tauchonki on 14 November.

### 5. Every producer, then worktrees

An exhaustive table of producers and consumers, each moved to one shared-backed
workspace constructor that keeps source and index isolation without copying
dependency payloads:

| Producer | Today | Change |
| --- | --- | --- |
| Host round preparation | private Mathlib unpack when sharing is skipped | step 3 view, step 4 linking |
| Bubble rounds | `bubble_work_cmd()` downloads and builds Mathlib in the container; per-round overlays copy up on write | container-visible read-only views; validate the real Bubble filesystem |
| Verification candidates | `verification_jobs._snapshot()` clones with `--no-hardlinks --dissociate` and copies `.lake` | worktree plus links |
| Recovery restore and delta integration | `repair_recovery.restore()` makes independent clones | worktree plus links |
| Review inputs | `_review_mathlib_source()` makes per-worker Mathlib source snapshots; TauCetiReview expects writable private copies | shared source; update the TauCetiReview contract |
| Mathlib publication | `publish()` clones project and dependency sources in staging | canonical source tree |
| Lake's own caches | `share_build_caches()` redirects workers to private stores | one host store |

Then switch worker checkouts to worktrees of one host clone, one per round:

- Worktrees share objects and refs but still materialize tracked source files;
  that is acceptable within the per-round quota.
- Use detached or uniquely named heads instead of resetting a shared `main`.
  Scope recovery and preservation refs per worktree, and audit shared
  configuration and hooks.
- Replace code that assumes `.git` is a directory (`prepare_checkout()`,
  `save_for_cleanup()`, direct `.git/index.lock` and `.git/hooks` access) with
  Git's path resolution. Worktree-aware recovery lands before worktree checkouts.
- At most (running rounds + 2) TauCeti source trees exist, against 93 permanent
  checkouts on tauchonki on 14 November.

### 6. Enforcement

- Per-round quotas on writable space (project quotas where the filesystem has
  them, otherwise a size-capped mount), killing the round when exceeded.
- Detection at round start validates the protected view root against its
  publication manifest. It never deletes through a resolved shared path when
  validation fails.
- Reporting separates named bytes, uniquely allocated inodes and filesystem free
  space: hard-linked files still count in `du`, so checkout size alone proves
  nothing.
- Enable per host once authoring, verification, recovery and review rounds keep
  completing, deletion keeps progressing, and private copies do not regrow.

## Reclaiming shared state

Deletion is the default. Only a live round's lease keeps shared state alive, and
leases are kernel locks that end when their holders exit, so they cannot leak.

- **Mathlib:** the two-version cap above. Nothing else is kept.
- **Artifact store.** Acquiring a reference, publishing and evicting all take the
  same per-object lock, and eviction rechecks eligibility under the exclusive
  lock, so an artifact cannot vanish between being found and being linked. The
  store records an explicit last-use time; link counts are evidence, not the
  refcount, because expanded entries add internal aliases.
- **Checkouts** are removed at the end of every round, with their links. A crashed
  round's worktree is removed once its lease is gone.
- **Git:** `git worktree prune` and ordinary `git gc`.
- **Space reservations.** Cold publication and builds reserve bounded space
  atomically before starting, and limit concurrent expansion, so many publishers
  cannot pass a free-space check together and exhaust the disk. Reservations are
  released after crashes. Budgets cover everything: archives, staging, trash and
  recovery, not each store separately.
- **Permissions.** Only the collector, holding the store's lock, restores
  directory write permission to remove shared entries.

## Acceptance tests

Concurrency and failure tests, not link-count samples:

- identical concurrent builds converge on one inode;
- a forced hard-link failure fails the round instead of copying;
- dependency changes and toolchain changes through the publisher;
- read-only rebuilds and reconfiguration under Lake;
- acquisition racing eviction;
- publisher and consumer death at every phase;
- a stalled deletion generation while others progress;
- a mixed-version live rollout.

## Out of scope

- Compressed `.ltar` archives keep their own bounded policy until restoration,
  mapping reconstruction and publication no longer need them.
- Interactive Codex and Claude sessions outside TauCetiWorker are not governed by
  this plan; the same principle should be written into their instructions.

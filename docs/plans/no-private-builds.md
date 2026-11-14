# No private copies of Mathlib, TauCeti or their builds

A worker host keeps exactly one copy of each immutable thing it builds against.
For every (Mathlib revision, toolchain) and every TauCeti artifact, the host keeps
one source tree and one set of build outputs, and every checkout reaches them by
reference. A worker must not be able to create a second copy. Making a copy is not
merely discouraged: no code path does it, so a failure to share stops the round
instead of silently making one.

This plan overturns earlier decisions. In particular it removes private fallback
for shared Mathlib ([shared workspace dependencies](shared-workspace-dependencies.md),
[cache preparation](cache-preparation-efficiency.md)), the warm private workspace
per worker, and whole-tree retention in recovery
([storage lifetimes](storage-lifetimes.md), [retained build trees](retained-build-trees.md)).

## Evidence

On 14 November 2026 tauchonki (3.5 TiB) filled to 100% several times within an
hour, with a load average of 200 to 740. Measured there:

- **Every worker checkout held its own unpacked Mathlib build**: 9.6 GiB under
  `.lake/packages/mathlib/.lake`, with a link count of 1 on its `.olean` files. It
  also held its own TauCeti build: 5.4 GiB under `.lake/build`. There were 93
  checkouts, so these copies alone came to about 1.4 TiB.
- **Sharing was available but mostly not used.** The shared Mathlib policy was
  `auto`. Of the 60 most recent per-worker decisions, 40 were `private` with the
  reason "sharing was skipped because the Mathlib checkout was absent", and 13 were
  `shared`. `shared_mathlib.view()` runs before `.lake/packages/mathlib` exists in a
  fresh checkout, so it chooses private, and `lake exe cache get` then unpacks a
  full private copy.
- **Writes reached about 36 GiB in 30 seconds**, measured from `/proc/<pid>/io`
  across all processes. The heaviest writers were worker loops and their reaped
  children unpacking build archives at round start.
- **Recovery and discard kept whole trees.** Three workers' `recovery-cleanup/discarded`
  held 83 GiB, mostly `checkout/.lake` and abandoned checkouts with their builds.
  On chungus2 the same queues had reached about 420 GiB, with single entries up
  to 64 GiB, almost all `.lake`.
- **Deletion then stopped.** A `tauceti-cleanup.service` run from 13 November
  05:25 UTC was still active 26 hours later, in kernel I/O wait. It held both
  `cleanup.lock` and the data root's `.recovery-housekeeping.lock`, so every
  in-process sweep skipped and the hourly timer could not start another run.
- **Not everything could be deleted.** Discarded checkouts kept overlayfs work
  directories (`tauceti-overlay-work/work`) with mode `000`, so they could not be
  removed until their mode was changed.

The `.ltar` archive pool is already shared by hard links (`build_caches.sync_pool`),
and on chungus2 a shared Mathlib view and Lake's artifact cache do reach many rounds.
The problem is that each of them is optional and has a private fallback.

## Invariants

1. **At most two Mathlib versions per host, ever.** A version is one immutable,
   read-only extracted build in the pool
   (`~/.cache/tauceti/mathlib-builds/entries/<key>`) plus its source tree and
   archives. The host holds the version current TauCeti `main` pins and the one
   before it, and never a third, not even briefly. A checkout's
   `.lake/packages/mathlib/.lake/build` is a view of one of them, never a copy. If
   a view cannot be made, the round does not start.
2. **One Mathlib source tree per revision.** `.lake/packages/mathlib` is a
   read-only shared source tree for that revision, never a fresh clone per checkout.
3. **TauCeti build outputs are content-addressed.** Restored and built `.olean`
   files live once in Lake's artifact cache and are hard-linked into checkouts
   (`LAKE_ARTIFACT_CACHE`, `LAKE_RESTORE_ARTIFACTS`). An archive is unpacked once,
   into that store, never into a checkout.
4. **One TauCeti object store per host.** Checkouts are worktrees of one host-level
   clone, or clones with git alternates on it. A worker never owns a full clone.
5. **Recovery saves source, never trees.** A recovery record holds a commit bundle,
   patches and bounded untracked files. It never materializes or retains a checkout,
   and never retains `.lake`. Once the source is saved, the tree is deleted
   immediately instead of being kept for 24 hours.
6. **TauCeti versions are bounded by running rounds.** Long-lived TauCeti state is
   one object store and two read-only reference builds: current `main` and the
   previous `main`. Every other TauCeti worktree and every branch-specific build
   output exists only while a round uses it.
7. **Deletion cannot stall.** No collector run, and no holder of the housekeeping
   lock, can block deletion for longer than a bounded lease.

## Design

### Mathlib: required, never private

- Remove the `off`/`auto` policy modes and the private branch of
  `shared_mathlib.view()`. Sharing is the only mode. A host that cannot share (no
  rootless overlay or symlink capability, integrity failure, safety stop) runs no
  rounds that need Mathlib, and reports why.
- Fix the ordering bug. Resolve the Mathlib package from `lake-manifest.json` and
  link `.lake/packages/mathlib` to the shared source tree for that revision
  *before* `view()` runs. `view()` must never see "checkout absent" for a manifest
  that names Mathlib.
- Publishing an entry stays as today (one publisher per key under the existing
  key locks). Only the publisher runs `lake exe cache get` for Mathlib, into
  staging; checkouts never do.
- Delete `build-before-sharing`, the private-fallback cleanup in `view()`, and the
  `TAUCETI_SHARED_MATHLIB_BUILDS` and `TAUCETI_SHARED_MATHLIB_WORKERS` overrides.
  Remove the dead `TAUCETI_MACHINE_LAKE_CACHE` drop-ins from Fleet hosts.

### TauCeti builds: content-addressed only

- Always set `LAKE_ARTIFACT_CACHE=1` and `LAKE_RESTORE_ARTIFACTS=1`, with the
  artifact store in the host build-cache root, not only inside
  `host_build_cache_context()` for the cache service.
- `expanded_cache` expands each CI archive once, into the store. Hydration into a
  checkout is linking only. Remove any path that writes archive contents into a
  checkout.
- A round's own new outputs go to the store through Lake as usual, so two branches
  share every module they have in common.

### TauCeti versions

Many TauCeti versions are in flight at once, one per branch, but almost all of
their build is shared with `main`.

- **Source.** One object store per host. A worktree exists only for the duration
  of a round: `git worktree add` at round start, removed at round end. Two
  read-only reference worktrees track current `main` and the previous `main`. At
  most (running rounds + 2) TauCeti source trees exist, against 93 permanent
  checkouts on tauchonki today.
- **Builds.** The two reference worktrees are the only long-lived TauCeti builds.
  Their `.lake` links the artifact store, so `main`'s modules keep a link count of
  at least 2 and stay. When `main` advances, the older reference is dropped and
  its modules become collectable unless the new `main` still uses them.
- **Branch deltas.** A round links `main`'s modules and builds only what its branch
  changes. Those outputs are referenced by that round alone; when its worktree is
  removed their link count drops to 1 and the collector deletes them at once. The
  next round on the same PR rebuilds its changed modules.
- **Downloads** of a PR revision's outputs from the CI cache go into the same store
  and follow the same rule.
- **Bound.** Two full TauCeti builds plus the deltas of running rounds. A delta is
  at worst a full rebuild (5.4 GiB on 14 November), so with 30 concurrent rounds
  the ceiling is about 11 + 30 × 5.4 ≈ 170 GiB, against about 500 GiB of private
  TauCeti builds on tauchonki that day.
- **Per-round cap.** A round whose new artifacts exceed a limit (for example
  6 GiB) is killed and abandoned, like a holder of an evicted Mathlib version, so
  the ceiling is a guarantee rather than an expectation.

### Checkouts and clones

- One host clone of TauCeti under the build-cache root. Worker checkouts are
  worktrees of it (`git worktree add`), never `clone --shared` or separate clones,
  so the object store exists once and `git gc` sees every reference.
- A checkout lives for one round (see [TauCeti versions](#tauceti-versions)). Its
  `.lake` holds only links, and its source is recoverable from git and the recovery
  record, so creating one costs a worktree add, not a 15 GiB rebuild.

### Recovery and retention

- `_retain` and `discard_record` stop moving whole checkouts. They save the source
  record, then delete the tree (with `.lake` only links, the tree is small).
- Recovery records drop `checkout/` and restore materializations. When work must
  resume, it restores the source record into a fresh worktree.
- `retained-checkouts/` and `discarded/` stop holding build state. The 24-hour tree
  lifetime is removed; the source record keeps its existing lifetime.

### Making copies impossible

Invariants 1 to 5 hold because no code path writes a copy. To keep it that way:

- **Read-only shared state.** Pool entries, shared source trees and the artifact
  store are made read-only (`0444` files, `0555` directories) after publication,
  so a stray `lake build` or `cache get` in a checkout fails instead of writing
  into shared state.
- **No unpacking in checkouts.** Inside a round, `lake exe cache get` and
  `cache unpack` go through the worker's shim, which refuses when the target is a
  checkout. The agent sandbox gets the same shim.
- **Detection as a backstop.** Round start checks the checkout. A private
  `.olean` under `.lake/packages/mathlib/.lake/build`, or an unshared Mathlib
  package directory, is an error: the build is deleted and the round fails with a
  reason, instead of building on top of it. The check is a link-count and
  device/inode test, not a traversal.
- **Budget alarm.** The worker reports its checkout's allocated size each round.
  A checkout over a few GiB is reported as a regression in `workers status` and in
  Fleet telemetry.

### Lifecycle of shared state

Deletion is the default. The only thing that keeps shared state alive is a live
round using it right now, shown by a `flock` lease that the kernel releases when
the process exits, so a reference can never leak. Checkout markers, idle
checkouts, old branches and recovery records protect nothing.

- **Mathlib: a hard cap of two versions, enforced before publication.** Per host
  (not per toolchain), only the version current TauCeti `main` pins and the one
  before it may exist: entry, source tree and archives together. Publishing a
  third version first evicts the oldest. Any round holding a lease on it is
  killed and abandoned with no recovery (no source record, no retry), and the
  version is then deleted. Only after that does publication start. Abandoned work
  is an accepted cost; holding a third version is not.
- **Other versions are refused, not rebuilt.** A round whose checkout pins any
  Mathlib version outside the two is not run: it neither republishes nor builds
  that version, and the task is dropped with that reason. A branch behind `main`
  is rebased onto current `main` (which needs only the current version) or
  abandoned.
- **Checkouts hold no references between rounds.** At the end of every round the
  checkout's `.lake` links into shared state are removed. Re-linking at the next
  round start is cheap. So outside active rounds nothing references the artifact
  store or a Mathlib entry.
- **Artifact store: link count is exact.** With checkouts unlinked between
  rounds, an artifact with `st_nlink == 1` is unused. The collector deletes
  unused artifacts older than a day, and oldest first whenever the store exceeds
  its budget.
- **Shared git store.** `git worktree prune` and ordinary `git gc`. Checkouts are
  worktrees, not `clone --shared`, so `gc` sees every checkout's references.
- **Checkouts.** Removed at the end of every round, with their links; a crashed
  round's worktree is removed by the next collector run once its lease is gone.
- **Excess is an error.** More Mathlib versions than allowed, or a store over its
  budget, is reported as a regression in `workers status` and Fleet telemetry, not
  only as a warning. If live rounds alone exceed the budget, new rounds wait for
  space rather than letting the disk fill.
- **Permissions.** Shared files and directories are read-only. Only the collector,
  holding the store's lock, restores directory write permission to remove an entry.
- **Races.** A round links a Mathlib entry only under its shared lease. Eviction
  takes the exclusive lock, killing any round that still holds a shared one, so a
  version never survives eviction because a round was using it. For the artifact
  store, an eviction racing a new link only costs the store its name for that
  file; the checkout's link stays valid and the next round re-fetches it.

### Deletion that cannot stall

- `tauceti-cleanup.service` gets `TimeoutStartSec` and always runs with
  `--max-seconds`, like the fallback collector
  ([#731](https://github.com/TauCetiProject/TauCetiWorker/pull/731)).
- The housekeeping lock records its holder and start time. A holder older than its
  lease, or whose process is gone, is reported, and the next sweep may proceed.
- Deletion sets owner write and execute permission on directories before removing
  them, including overlayfs work directories left with mode `000`.

## Migration

1. **Stop the bleeding.** Fix the `view()` ordering bug, make sharing `required`
   on Fleet hosts, and enforce the two-version cap (with eviction killing leased
   rounds) first. Add the round-start detection, in report-only
   mode. Bound the collector. These are small and can ship at once.
2. **Content-addressed TauCeti builds** on all hosts. Measure link counts and
   checkout sizes before and after.
3. **Shared clone and worktree checkouts.** Existing per-worker clones are retired
   by the collector once no round holds them.
4. **Source-only recovery.** Remove tree retention and materializations; existing
   records keep their source and lose `checkout/`.
5. **Enforcement.** Make shared state read-only, ship the unpack shim, and switch
   detection from reporting to failing the round.

Each step deploys live, without draining the fleet.

## Out of scope

- The Lean toolchains in `~/.elan` are already one copy per toolchain. Lake's
  artifact cache under a toolchain is already shared by hard links.
- Interactive and agent sessions outside TauCetiWorker (for example per-task
  worktrees created by Codex or Claude sessions) are not governed by this plan.
  The same principle applies to them, and should be written into their
  instructions.

## Open questions

- Whether a symlinked `.lake/packages/mathlib` works with every Lake operation the
  agents use, or needs a read-only bind or overlay mount instead.
- How agents that legitimately edit a dependency (rare) get a writable copy
  without violating the invariant: a separate, explicitly labelled, size-bounded
  path, deleted at round end.
- Whether `.ltar` archives are still needed once the artifact store and pool
  entries exist, or can be dropped after expansion.

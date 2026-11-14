# Shared extracted Mathlib builds

A host holds Mathlib once per version, and every round that needs Mathlib uses that copy
through a per-round view. There is no private Mathlib: a round that cannot get the shared
view does not start, and says why. Projects that do not depend on Mathlib are unaffected.
Reviews retain their existing execution path.

Sharing is required by default, including on a host without `policy.json`; `auto` is an
old spelling of `required`. `off` (the `TAUCETI_SHARED_MATHLIB_BUILDS` environment variable,
or `mode` in the pool's `policy.json`) is an operator's emergency switch: it blocks every round
that needs Mathlib until sharing is restored. An unreadable or invalid policy blocks too.
`TAUCETI_SHARED_MATHLIB_ROOT` selects the pool; it is pinned to the login user's home
before worker HOME isolation. An optional comma-separated `TAUCETI_SHARED_MATHLIB_WORKERS`
allowlist (or `workers` in `policy.json`) blocks the workers it leaves out. `policy.json`
also accepts an `expires_at` UTC epoch with `expiry_mode` (default `required`).

Run `tauceti doctor` to exercise Linux user namespaces, bubblewrap overlay support and
private copy-up. No root daemon is required. A host without them runs no rounds that need
Mathlib; the reason names the administrator action.

## Layout

Everything below lives in the pool's `v4/` directory and is written only by the worker's
publication code. Anything else in the pool is an older layout: unleased old entries are
deleted by collection, never migrated, and leased ones are evicted first when the version
cap needs their slot.

- `sources/<rev>/<package>/` is the source tree of Mathlib `rev` and of every package its own
  `lake-manifest.json` requires (Batteries, Aesop, Qq, ProofWidgets, Plausible and the rest):
  a shallow checkout detached at the pinned commit, with `origin` set to the URL Lake expects
  (`LAKE_PKG_URL_MAP`, else the manifest's). Every write bit is removed, and the index is
  refreshed afterwards so Lake's `git diff` never needs to write. Each package has an empty,
  read-only `.lake/build`: the mount point of its build.
- `entries/<key>/build/<package>/` is the extracted build of every closure package for one
  key, with `ready.json` recording its inventory. File modes stay as Lake extracted them,
  because Lake rewrites `.hash` files in place when it saves artifacts; the pool is
  read-only inside every view, and builds are only reachable as overlay lowers.
- `locks/`, `staging/` and `trash/`.

A round resolves Mathlib's pin from the checkout's tracked `lake-manifest.json` (no Lake, no
network) and attaches the checkout before any Lake run: each closure package's
`.lake/packages/<package>` becomes a symlink to its shared source, and a private package
directory is moved aside whole into `.lake/tauceti-mathlib/` and deleted once unused. Lake
resolves the link, so every Lake run, in a view or not, uses the shared source, and a
revision or URL mismatch fails on the read-only tree instead of fetching. Links into the
pool for packages outside the current closure are removed, so Lake materializes those
privately as ordinary root dependencies. Every closure package must be pinned exactly as
Mathlib's own manifest pins it; another dependency graph cannot share Mathlib.

## Builds and keys

The key covers only what changes the closure's build outputs: the revisions of Mathlib and
its closure, the Lean toolchain's identity (`lake env lean --version` and the `Init.olean`
digest), the artifact formats it produces, architecture and system, and compiler-selecting
environment (`LAKE_OVERRIDE_LEAN`, `LEAN`, `LEAN_SYSROOT`, `LEAN_GITHASH`, `LEAN_CC`,
`LEAN_AR`, hashed). TauCeti's lakefile, manifest and toolchain text, root-only dependencies,
branches, forks, Git remotes and cache transport settings (`LAKE_CONFIG`, `LAKE_CACHE_*`,
`MATHLIB_CACHE_*`) do not split it. Several keys can share one source revision.

A miss publishes from a staging workspace: a minimal `lakefile.toml` requiring Mathlib, the
closure's manifest entries, and package links to the shared source, with each package's
read-only `.lake/build` bound to a writable staging directory. `lake exe cache get` there
fills the staging build of the whole closure and nothing else; Mathlib's `.olean` and
`.ilean` outputs are verified before the tree is synced and renamed into `entries/`. It never
publishes an agent's build directory. A per-key publisher lock serializes builders separately
from reader leases. `ready.json` records each regular file's inode, mode, link count, size,
mtime and ctime; every reuse checks that inventory. Links, special files and hardlink aliases
are rejected. A damaged published lower is preserved for evidence and writes
`safety-stop.json`, which blocks every later round until an operator investigates, removes
the stop file and restores the policy.

## Views

A view is a rootless bubblewrap mount namespace with the pool read-only and, on top, one
OverlayFS mount per closure package: the published build as lower, a private upper and work
directory in `.lake/tauceti-mathlib/<generation>/<package>/`, and the read-only
`sources/<rev>/<package>/.lake/build` as target. Rounds on different keys of one revision
mount different builds at the same path in their own namespaces. Outside a view the build
directory is empty and read-only, so an unprefixed build fails instead of copying. There is
deliberately no PID namespace: agents and their tools see ordinary host PIDs and `/proc`.
The upper uses OverlayFS `volatile` mode; a small mount helper is compiled once with `cc`
beside the pool and its preload is removed before running agent commands.

Each round starts from a fresh generation; the previous one is deleted once no namespace
holds its lease. Running readers hold shared leases on the source and on the build; the
sandbox and cache fetches inherit them, so collection and eviction cannot race a surviving
process. Lock waits are bounded to 25 minutes, then the round stops with the reason.
`.lake/tauceti-mathlib/view.json` records the current view (key, revision, generation).

Agents never fetch or resolve Mathlib: the agent-facing `lake` wrapper refuses
`lake exe cache get` and `lake update` inside a view, and refuses every Lake command while
the checkout's Mathlib pin differs from the view's. Rebase and repair rounds whose pin is
refused prepare main's Mathlib before the agent starts, so integrating main brings the pins
back into agreement. Verification snapshots keep the package links and copy no Mathlib
bytes; verification continuations enter their own view.

## Reclaiming

At most two Mathlib versions exist per host. A version is a revision: its source tree and
every build of it. Admitting a new revision first evicts old-layout builds, then versions
outside current and previous `main` (plus recent bump pins), then the oldest; rounds holding
an evicted version are killed and abandoned. Eviction hides the version before deleting it.
Collection reclaims trash and dead staging, entries unused for seven days (or oldest unused
under disk pressure, keeping the free-space reserve of 10% capped at 20 GiB), and source
trees with no builds left. Leased state is never collected.

The activity preparer collects small local preparation receipts; status reads only that
summary (shared, preparing, blocked and idle counts, cache hits, preparation time and
reasons). `results.jsonl` in the pool contains content-free invocation and cleanup receipts
with native round IDs and upper file counts and bytes.

Existing `resources.jsonl` receipts include round `read_bytes` deltas from
`/proc/self/io` for the supervisor and reaped children. Live/unreaped descendants
are not included in this storage-read accounting. Separately, `worker_scope`
reports optional per-device I/O and `pressure_stall_us` totals for the worker's
scope, including background verification. These are cumulative scope readings,
not round deltas or host-wide/co-tenant measurements. Fields are unknown when
unavailable. Join resource `activity_id`, shared receipt
`round_id`, and existing audit/round phase completion before comparing runs.
The [Linux I/O accounting manual](https://man7.org/linux/man-pages/man5/proc_pid_io.5.html)
describes the kernel read counter and its scope.

## Rollout

Prewarm the actual keys on a host before requiring the new layout there, then start with one
host's workers. Deploy every worker on a host together: an older worker's round that still
holds an old-layout build is killed once a new version needs its slot. Keep funding and
worker capacity unchanged, and use only naturally selected work.

Known remaining copy: with `LAKE_ARTIFACT_CACHE=1`, Lake saves Mathlib's outputs into each
worker's `LAKE_CACHE_DIR` the first time that worker builds against a new version, because
its hard links cannot cross the overlay. Publishing those mappings with the entry is a
follow-up.

The [historical #311 validation record](evidence/shared-mathlib-311.json) predates
#364. Its containment deployment requirements are retired; it establishes neither
deployment nor reactivation readiness for this baseline.

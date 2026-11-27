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

On macOS, which has neither rootless mount namespaces nor overlays, a view is made of APFS
clones instead ([macOS](#macos-views-made-of-apfs-clones)). There `tauceti doctor` clones a
small read-only tree on the pool's filesystem, writes to the clone and checks that the
original is unchanged. The pool and the worker checkouts must be on the same APFS volume.
Any other system runs no rounds that need Mathlib.

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
mtime and ctime; every reuse checks that inventory (on macOS a content digest backs it:
see [below](#macos-views-made-of-apfs-clones)). Links, special files and hardlink aliases
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

### macOS: views made of APFS clones

`clonefile(2)` gives the two properties the overlay gives: the host stores one build of
Mathlib per version, because a clone shares its original's blocks copy-on-write, and a
round writes only to its own copy. A view there is prepared like this:

- Each closure package's shared source (without its build directory) and its published build
  are cloned file by file into `.lake/tauceti-mathlib/<generation>/<package>/`, the build at
  `.lake/build` inside it. Directories become writable and build files regain owner write;
  source files keep their read-only mode, and `.git` stays read-only throughout, so Git can
  neither fetch nor move the checkout. The index is refreshed once, because a clone is a new
  inode. `clone.json` in the generation marks the end of cloning.
- `.lake/tauceti-mathlib/current` is then pointed at the generation, and each
  `.lake/packages/<package>` at `.lake/tauceti-mathlib/current/<package>`. The link targets are
  therefore the same in every round. This is the view: there is no namespace, and the argv
  prefix only sets `TAUCETI_SHARED_MATHLIB_CLONE` to the generation (callers and the agent's
  `lake` wrapper know a view by its non-empty prefix, as on Linux).
- When the view ends, the package links name the shared sources again, whose build
  directories are empty and read-only, and the generation is deleted unless a surviving
  process still holds its lease. So outside a view an unprefixed build fails instead of
  copying, as on Linux. A worker killed inside a view runs no cleanup: its checkout keeps
  the links to the dead round's clones until the next round in that checkout attaches (which
  replaces the links) and deletes the generation.

Cloning never falls back to copying bytes. Where `clonefile` fails (a filesystem other than
APFS, or the pool and the checkout on different volumes) the round does not start, and the
reason says so. Publication has no bind mounts either: the staging workspace's packages are
clones of the shared source, `lake exe cache get` fills their own build directories, and
those are moved into the staging build, verified and published as on Linux. One step is
added before that: Lake's artifact cache hard-links outputs it builds (Mathlib's `cache`
executable, for one) into `LAKE_CACHE_DIR`, and restores cached outputs by hard link. With
the staging tree and the Lake cache on one volume those links succeed, so every staged file
with more than one name is replaced by a clone of itself, which has its own inode and
shares the blocks. The inventory's refusal of hardlink aliases is unchanged and still
applies to the result.

**Integrity of an entry.** On one Mac (macOS 26.5.1, APFS) something outside the worker
updated the ctime, and only the ctime, of most files of an entry a few minutes after each
publication; the cause is not known. The inventory comparison alone would then stop
sharing on every such host. So on macOS `ready.json` also records the SHA-256 of every
file's content (`digest`, `digests`), taken at publication. A reuse first compares the
inventory as everywhere. If that differs, and the same files are present, and each differs
from its record in ctime at most, the content of every file whose ctime moved is read and
compared with its digest. If all match, the entry is accepted and `ready.json` is rewritten
with the new ctimes (and `content_verified_at`), so the reading is paid once per touch and
not once per round. Any other difference (inode, mode, link count, size, mtime, a missing
or additional file), content that differs, and an entry published without digests are an
integrity failure and a safety stop, as on Linux. Linux records carry no digests and Linux
never takes this path.

Cost, for one Mathlib build (137,345 files, 7.3 GB), measured on a Linux x86-64 host with
SHA extensions: about 6 s to hash everything from the page cache, 24 to 72 s from a cold
cache on a busy disk; `ready.json` grows from 25 MB to 45 MB. Not yet measured on a Mac.
Publication pays it once more than before, and a reuse only after a touch.

What this does and does not detect: any write to a published file moves its ctime, which
the file's owner cannot set, so a changed file is always read and compared, including one
whose size and mtime were restored. As on Linux, it does not detect damage that leaves a
file's status untouched (failing storage), and it does not defend against the host's owner,
who can rewrite `ready.json` itself.

What is weaker than on Linux:

- No read-only mount protects the pool. A published build has every write bit removed
  instead (its clones get owner write back), and nothing in a checkout names the pool's
  builds. This guards against accidents, as file modes do; the inventory in `ready.json`
  still detects a changed file at the next reuse and stops sharing.
- Eviction cannot find and kill the rounds that hold a version, because that uses `/proc`.
  A round that must evict a version another round still leases waits, then stops with the
  reason; the eviction completes once the holder has ended.
- A clone keeps its original's blocks allocated. Deleting an entry frees its space only once
  no clone of it remains, which is why a round keeps its leases for its whole life (leased
  entries are never collected) and why a generation is deleted when its view ends. The
  receipts' upper file counts and bytes are the regular files a round created or rewrote in
  its clones (changed after `clone.json` was written). The per-round private-bytes cap
  (docs/workers.md) sees the clones as ordinary private files at their full size; they are
  part of the baseline taken before the agent starts. If macOS updates the ctimes of a
  round's clones as it was seen to do in the pool, those receipts overstate what the round
  wrote; nothing but reporting depends on them.

Each round starts from a fresh generation; the previous one is deleted once no namespace
holds its lease. Running readers hold shared leases on the source and on the build; the
sandbox and cache fetches inherit them, so collection and eviction cannot race a surviving
process. Lock waits are bounded to 25 minutes, then the round stops with the reason.
`.lake/tauceti-mathlib/view.json` records the current view (key, revision, generation).

Agents never fetch or resolve Mathlib: the agent-facing `lake` wrapper refuses
`lake exe cache get` and `lake update` inside a view, and refuses every Lake command while
the checkout's Mathlib pin differs from the view's. It also answers TauCeti's own
`lake cache get` as done once the worker has installed those outputs (docs/workers.md, after
the description of hydration). Rebase and repair rounds whose pin is
refused prepare main's Mathlib before the agent starts, so integrating main brings the pins
back into agreement. Verification snapshots keep the package links and copy no Mathlib
bytes.

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

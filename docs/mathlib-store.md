# Read-only Mathlib store for sandboxed rounds

A Bubble work round starts in a fresh container and unpacks Mathlib's build
(about 7 GB) into it before the agent runs. `TAUCETI_MATHLIB_STORE` names a
directory on the host that holds that build once per Mathlib revision. A work
round then mounts it read-only and links the entry its checkout pins in place of
the unpack, so the containers on a host share the files on disk and in the page
cache.

The worker only reads the store. Something outside the worker builds entries and
removes them, to the layout below; this page is the contract between the two.
Unset, or not an absolute path to an existing directory, the variable changes
nothing. A round whose pins match no entry runs exactly as it does without the
store. Review and progress rounds do not compile and never mount it.

## Layout

```
$TAUCETI_MATHLIB_STORE/                  mounted read-only at /opt/mathlib-store
  <rev>/                                 a Mathlib checkout at commit <rev>
    .tauceti-store-ready                 written last
    lean-toolchain                       the checkout's own
    lake-manifest.json                   the checkout's own
    .lake/build/                         Mathlib's build
    .lake/packages/<name>/.lake/build/   the build of each Mathlib dependency
```

An entry is a Mathlib checkout exactly as its builder left it, named by the
commit it is at: forty lowercase hexadecimal digits, the value of `rev` for the
package named `mathlib` in a TauCeti checkout's `lake-manifest.json`. The worker
reads five things in it:

| Path | What the worker takes from it |
|---|---|
| `.tauceti-store-ready` | Its existence as a regular file makes the entry usable. The contents are not read; by convention they are the revision. |
| `lean-toolchain` | The toolchain the builds were made with, compared as a stripped string with the round's own `lean-toolchain`. |
| `lake-manifest.json` | The revision of each dependency whose build the entry holds. |
| `.lake/build/` | Linked as Mathlib's build directory. It has `lib/lean/Mathlib.olean`, `lib/lean/Mathlib/…` and the `cache` executable's outputs. |
| `.lake/packages/<name>/.lake/build/` | Linked as the build directory of the dependency `<name>` (`batteries`, `aesop`, …). Optional per dependency. |

Everything else in an entry (sources, `.git`) is ignored. So is any name in the
store that is not a revision, but the whole directory is visible inside every
container, so download caches, clones and logs belong somewhere else.

## What a round does with it

Inside the container, before any cache fetch, the round has Lake clone the
checkout's packages (`lake env true`) and runs
`/opt/round/tauceti-mathlib-store`. For the Mathlib revision the checkout pins,
that script uses the entry `<rev>/` only if the marker exists, `.lake/build/` is
a directory, and the entry's `lean-toolchain` equals the checkout's. `.olean`
files are specific to a toolchain, so a matching revision alone is not enough.

For a usable entry it makes `.lake/packages/mathlib/.lake/build` a symbolic link
to `/opt/mathlib-store/<rev>/.lake/build`. For each dependency that the entry's
manifest and the checkout's manifest pin at the same revision, and whose build
the entry holds, it links `.lake/packages/<name>/.lake/build` likewise. A
dependency pinned differently is left to the usual unpack. A package whose build
directory already exists is left alone, and dependencies are linked only when
Mathlib is.

`lake exe cache get` still runs, once. It finds every linked file already
unpacked; it still wants Mathlib's `.ltar` archives present, which Bubble's
shared download cache normally holds. If the command fails with the links in
place, the round removes them and fetches as it does without the store.

### `LAKE_ARTIFACT_CACHE`

Bubble exports `LAKE_ARTIFACT_CACHE=true`. With the artifact cache writable,
Lake hashes each output it replays and copies it into the cache, writing beside
the output as it does, which a read-only build directory refuses. A round that
linked an entry therefore leaves `LAKE_ARTIFACT_CACHE` unset for its bootstrap,
its agent and, through `~/.profile` and `~/.bashrc`, the login shells the agent
opens. Unset is not `false`: unset, Lake still reads its cache, which is how
TauCeti's own outputs from `lake cache get` are restored; `false` turns that off
as well. The cost is that modules the round compiles are not kept in Lake's
local store, so returning to an earlier source state within the same round
recompiles them.

`tauceti-mathlib-store` refuses to create a link while the variable is set in
its own environment, so a link never appears under a writable artifact cache.

### Pins that move during a round

The links are made once, at the start. A round that moves the pins, typically by
merging a `main` that has bumped Mathlib, is told in its prompt to run
`/opt/round/tauceti-mathlib-store` again before fetching: it removes links that
no longer match and links the new pin's entry when the store has one. Until
then, a write through a stale link fails with a permission or read-only error.

## Building an entry

The worker ships no builder. Whatever builds entries keeps to this:

1. Build on the platform the containers run. The entry includes native outputs
   such as the `cache` executable, so a store built on macOS does not serve
   Linux containers.
2. In `<rev>/`, a checkout of Mathlib at that commit, with the checkout's own
   `lean-toolchain` selecting Lean, run `lake exe cache get` and then
   `lake build --no-build Mathlib`. The second command matters: the first time
   Lake replays an unpacked build it rewrites a few `.hash` files, and it must
   have done so before the files are mounted read-only.
3. Run both with `LAKE_ARTIFACT_CACHE` unset. A worker's own environment sets
   it to `1`, under which Lake may keep outputs in its artifact store rather
   than in the build directory.
4. Leave every file readable, and every directory searchable, by the
   container's user. Removing write permission from the build directories is
   optional; the mount is read-only either way.
5. Write `.tauceti-store-ready` last. Once it exists, nothing listed in the
   table above changes; the marker's own timestamp is the builder's to use. A
   failed build leaves no marker.

## Removing an entry

The worker takes no lock or lease on an entry, because the container, not the
host, chooses it. An entry can be in use until the end of any round that
started while its marker existed, on a checkout that pinned its revision.

- Remove an entry only once no branch a round can start on (TauCeti `main` and
  the open pull requests of the workers that share the store) has pinned its
  revision for longer than the largest `TAUCETI_ROUND_TIMEOUT` among those
  workers (default 5400 seconds), with a margin for the container to be torn
  down.
- To withdraw an entry that is still pinned, for example to rebuild it, delete
  the marker first, wait that long, and only then delete or rewrite the rest.

Each entry is about 7 GB.

## Relation to shared Mathlib builds

[Shared extracted Mathlib builds](shared-mathlib.md) serve host rounds. There
the worker owns the pool: it publishes an entry from the checkout it is about to
use, keyed by a digest of that checkout's whole dependency configuration, holds
a lease while a round runs, and gives the agent a private copy-on-write view
through OverlayFS.

A Bubble round has no checkout on the host to key on, and its container can
neither publish to a host pool nor mount an overlay over one. So the store is
published from outside the worker, keyed by what a container can read from its
own checkout (the Mathlib revision, checked against the toolchain), and consumed
through plain links with no private upper layer and no lease. An entry here is a
whole checkout, because that is where Mathlib's cache tool and the settling
replay have to run; the pool's `entries/<key>/build` is a build directory moved
out of one. The two are separate directories with separate lifetimes, and
neither reads the other: `TAUCETI_SHARED_MATHLIB_*` has no effect on a Bubble
round and `TAUCETI_MATHLIB_STORE` none on a host round.

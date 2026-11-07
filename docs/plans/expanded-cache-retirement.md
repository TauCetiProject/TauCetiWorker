# Cache retirement: remove the unused native experiment, retain the useful CI pool

The supported final configuration is stock Lean/Lake, the existing verified
expanded CI artifact pool, shared immutable Mathlib build views, and private
writable metadata. A new compiled-cache replacement or full expanded-pool retirement
is not part of the remaining implementation.

## Retired

The unused experimental `~/.cache/tauceti/lake-native` store and
`~/.local/share/tauceti/native-lake` router have been removed. The one-time operation
checked producers, actual consumers, ownership and managed-path identity, then
serialized detach with existing cleanup and removed the retired sibling. Subsequent
ordinary worker rounds showed neither path regrowing. No patched Lean/Lake runtime
is selected or retained through that router.

The native operation removed 466,126 canonical file names, with 30,559,416,320
named allocated bytes and a 14,667,259,904-byte final-link allocation estimate.
These are different quantities. Neither is an exact physical saving: private hard
links and shared extents can retain bytes, and other work changed free space during
cleanup. The operation and acceptance receipts are under
`~/.local/state/tauceti-fleet/storage-plans-implementation-20261107/`.

## Retain the verified expanded CI pool

`~/.cache/tauceti/builds` is an active supported cache. Recent preparations reused
roughly 25,000–30,000 extracted artifacts each, often without any new expansion.
Its immutable CI artifacts and fresh mappings are distinct from locally built
worker outputs and from the shared Mathlib build views. Removing it would forfeit
working download/decompression reuse without an established stock replacement.

A scoped 7 November inventory counted approximately 37.73 GB of allocated bytes
across unique inodes within the pool, before accounting for shared physical extents
or hard links outside the pool. The accounting order was entries, artifacts,
archives, helpers, Mathlib archives, revisions, downloads and staging:

| Component | Allocated bytes on additional inodes |
| --- | ---: |
| Expanded bundle entries | 23,979,188,224 |
| Canonical extracted artifacts | 0 |
| Canonical compressed archives | 110,592 |
| Toolchain hash helpers | 11,716,972,544 |
| Mathlib download archives | 1,958,436,864 |
| Revision hints | 76,316,672 |
| Active download/staging trees | 0 at observation |

Canonical artifacts named another 22.29 GB, but those inodes were already counted
through bundle entries. Likewise, nearly all canonical archives were already
linked through entries. Adding these directory totals would double-count them.
The aliases are intentional lookup names, not evidence of a second full store.
Helper executables include older toolchain-path variants; existing helper cleanup
already applies. No new helper retention duration is selected here.

Existing ownership and per-entry/download/publication locks remain in force.
Existing inode-aware collection removes eligible bundles, unreferenced canonical
aliases and expired helpers under current policy. Mutable mappings remain private;
only verified CI content is shared. The native experimental store's retirement does
not change this trust boundary or make locally built mappings trusted CI evidence.

## Completion and operating checks

Unused native-cache and router retirement is complete. Retaining the useful expanded
pool is the selected supported design, not an unfinished replacement rollout.
There is no pending requirement to deploy an upstream patch, add a second artifact
format or wait for a future Lake release to finish this cleanup.

[Retained build disposal](retained-build-trees.md) releases old private aliases and
scratch. [Source sharing](shared-workspace-dependencies.md) avoids repeated review
source payloads. Their savings must not be added to removed canonical names as if
all three represented independent physical allocations.

Normal checks remain useful work through stock Lake, fresh verified mappings,
private write isolation, normal collector progress and absence of retired-root
regrowth. Full removal of an active cache would be a separate future request that
first establishes an equally useful replacement; it is not an open implementation
step in this plan.

# Shared sources and dependency layers: completion

The supported design uses immutable shared Mathlib build versions with private
writable build deltas, and exact shared Mathlib review sources with private
writable source copies. New commits publish new versions during ordinary work;
running consumers never need a global quiet period.

## Already deployed

Both running workers reported reuse of the same shared Mathlib build version in
the 7 November production observation. This works with stock Lean/Lake. Private
fallback remains available for incompatible environments; policy and safety-stop
checks retain their existing meaning.

[Worker #601](https://github.com/TauCetiProject/TauCetiWorker/pull/601) supplies
scoped, multi-version file-lock leases for existing shared repository consumers.
[Worker #604](https://github.com/TauCetiProject/TauCetiWorker/pull/604) supplies
owned staging and crash cleanup for Bubble review inputs. Both are merged and
deployed. [Retained build disposal](retained-build-trees.md) handles old generated
deltas rather than retaining a second warm build indefinitely.

## Review source implementation awaiting merge approval

[Review #218](https://github.com/TauCetiProject/TauCetiReview/pull/218) implements
the exact-source cache and its actual consumer.
[Worker #609](https://github.com/TauCetiProject/TauCetiWorker/pull/609) enables it for
host reviews through an environment variable, pins the installed collector
dependency and calls that collector from existing cleanup. Host reviews continue
to use Review main unless `TAUCETI_REVIEW_REF` selects another revision; the
versioned `exact-sources-v1` contract keeps producer and collector compatible. Older explicitly selected Review versions
ignore the environment variable and continue private preparation.

Review reads the dependency pin from its already fetched PR-head manifest. There
is no duplicate GitHub lookup or cross-process reader lease. Explicit
`--mathlib-dir`, `--no-mathlib` and caller workspaces keep their precedence.

The opaque store lives below the existing repository-cache root in
`review-mathlib/exact-sources-v1`. Its ownership directory, exact-pin readiness
records and reserved staging/trash names establish what it owns. Existing
snapshot collectors do not enumerate this namespace.

One stable nonblocking flock per commit is sufficient: every reader takes SH
before looking up the published tree; publication and collection take EX before
looking up or changing it. Lock inodes are never unlinked. Network fetches run
privately outside cache locks. A miss fetches through the existing path, then may
seed the cache. Publication checks exact clean HEAD, copies source without Git,
finishes permissions and durability, and renames atomically. Published versions
are never rewritten beneath readers.

A reader leases the source only while copying it into Review's owned workspace.
FICLONE shares extents where supported; ordinary copying remains available. Copies
have independent inodes and writable directories/files. Partial-copy permissions
are repaired even on failure, the partial destination is removed, and a private
fetch happens after the lease closes. Paid inference begins only after preparation.

Cleanup takes the same commit's EX lock without waiting, rechecks eligibility,
detaches into owned trash and deletes it. Other commits remain available. Interrupted
staging/trash is recoverable even if its payload marker was never written or has
already been deleted. Later acquisitions and existing Worker cleanup use this same
implementation. The one-day threshold matches the repository snapshot age
threshold. This exact-source cache has no selected-main pointer: an unleased pin
can expire even if used recently, causing an ordinary private refetch on a later
review. No new size/count cap, last-use policy, daemon or admission restriction is
introduced. Cleanup unit installation preserves explicit Review and repository
cache overrides; existing units receive these settings when reinstalled at rollout.

## Benefit and acceptance

A warm host review avoids one Mathlib fetch (about eight seconds in the fixture)
and shares about 105 MB of source payload with concurrent reviews. Finished review
workspaces are still deleted; source sharing is not a permanent per-worker saving.

An isolated tauchonki fixture retained 56 private inputs, using four concurrent
preparations as benchmark load. It made no model calls and changed no fleet setting.

- Exact source: 9,479 files and 104,707,612 logical bytes.
- All 530,824 warm-copy FICLONE attempts succeeded; all 56 private mutations were
  isolated from the canonical source. FIEMAP confirmed the same shared physical
  extent in the canonical source and sampled private copies.
- Cold fetch plus publication: 10.04 seconds. Warm preparation: mean 1.72 seconds
  at the fixture's concurrency, with no network fetch.
- Repeating that payload 56 times would name 5,863,626,272 logical bytes. Reflinks
  avoid repeated source payload, while metadata and later edits remain private.
  This is not a measured physical free-space delta or an estimate of normal review
  concurrency. The fixture was removed afterward.

The test suite covers exact-pin changes, concurrent versions, reader/GC exclusion,
process death, partial publication/deletion, copy errors, unsupported reflinks,
relative/unavailable cache paths and caller-workspace preservation. Before rollout,
merge Review #218 with its reviewed commit preserved in main history, verify
Worker's pinned commit is reachable there, then merge Worker #609. If Review is
squashed instead, repin Worker to the resulting main commit and repeat CI before
merging it. Keep normal admissions on; verify an actual host review and a normal collector opportunity after approval.

## Writable Git workspaces remain private

This plan does not introduce shared mutable Git checkouts, alternate-object
lifetimes or a second workspace overlay manager. In the two-worker sample,
TauCeti source plus Git named about 653–672 MB per checkout; Mathlib source plus Git
named about 171 MB. These are materially smaller than the build trees that dominate
worker storage, and named bytes do not establish exclusive physical duplication.
The existing writable checkout and Lake package paths remain the supported design.
They are a deliberate boundary, rather than a partly implemented Git-sharing path.

Evidence is under
`~/.local/state/tauceti-fleet/storage-completion-20261107/`: `production-probe.json`,
`private-sources.json` and `source-benchmark-final.json`. The final benchmark used
Review revision `ffcc3ae60a46065e1cdfc7ad22725003964953fd`, whose source copy/cache
implementation is also present in the reviewed `5da3842` pin; the later changes
add unsupported-platform guards and classify a failed partial-input cleanup before
inference. `production-probe-corrected.json` uses each checkout as its toolchain
working directory; `stock-toolchain-proof.json` shows the Lean wrapper's stock
compiler target and Lake's direct stock binary identity. The first probe's compiler
lookup in the package directory was not evidence of a production compiler failure.

Current build-layer reuse, disposal and review-source sharing are the deliverables
of this plan. There is no pending proposal to mutate an immutable version in place
or drain the fleet to update it.

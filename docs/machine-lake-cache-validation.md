# Native Lake cache implementation validation

Evidence recorded on tauchonki on 6 November 2026. The implementation and live
rollout are verified across all 40 enabled workers. The broader retained-data
accounting scan is still running.

## Delivered changes

- [Worker integration](https://github.com/TauCetiProject/TauCetiWorker/pull/573):
  per-invocation native selection, private fallback, runtime installation/router,
  isolation at existing verification boundaries, and optional background cleanup.
- [Native Lake fixes](https://github.com/leanprover/lean4/pull/16301), with a
  [v4.36.0-rc2 backport](https://github.com/kim-em/lean4/commit/32e42ebe30ab58b6c4d3afac46222c9dac66eac3).
- [Production worker pin](https://github.com/TauCetiProject/TauCetiFleet/pull/126).
  Production uses a backport retaining the existing legacy-data-root support
  and deployed Lean-pressure changes. Fleet source files were preserved byte
  for byte; only the worker pin changed.

The installed native executable has SHA256
`bf99c2b9aecfc313892a7ed721b7fae7ca4b8581e54124fab70719f2ee626774`.
Compiler files and project toolchain pins were not replaced. The existing Lean
pressure wrapper remains in use; its forwarding sysroot resolves to the release
libraries and therefore shares the release's native namespace.

## Requirement audit

| Requirement | Evidence and boundary |
| --- | --- |
| Complete publication in batch, lazy and extra-destination downloads | Real paused HTTP and overlapping publisher regressions, including a killed publisher; paused extra-destination copies cover both freshly downloaded and already cached payloads. |
| Complete local artifacts and output/revision metadata | Paused binary/text local-copy regressions, including killed writers; concurrent JSON publication and cross-workspace native builds. Stock Lake fails the new local-copy regression by exposing partial final artifacts. |
| Eviction after lookup or before unpack is recoverable | Controlled disappearance regressions for restore, archive unpack and whole-store removal pass. Shared metadata becoming unwritable falls back to private operation. |
| Rebuilds and archive extraction preserve other aliases | Edit/rebuild/switch-back regression and real bundled leantar overwrite regression pass. Existing executable permissions are preserved. |
| Selection follows every actual Lake invocation | Old children, direct old executables, separate release identities, linked/overridden toolchains, explicit cache overrides and stock/patched cached-configuration alternation are covered. The public environment structure layout is unchanged. |
| Same-filesystem, restored-output prerequisites | Native selection checks filesystem/link support and effective restore settings; unsuitable or unsupported invocations keep private storage. |
| Avoid duplicated expansion in shared mode | Compatible native preparation bypasses the private expander. Existing expanded-cache tests pass for private mode. Mathlib's separate downloader retains its existing handling. |
| Warm existing outputs without bulk migration | A real up-to-date private build seeds and joins the native store; production workers populate it through ordinary work. Existing multiply linked private aliases remain for ordinary cleanup. |
| No new retention or admission policy | Both new age/size settings are unset in production. Tests cover unset, independent age/size, zero, invalid, dry-run and pinned-data cases. No new worker-count or admission limit is introduced. |
| Optional collection stays off paid startup/status paths | Existing background cleanup entry point performs inventory and selective collection. Detection shares the existing cache-preparation deadline. |
| Collection races do not become work failures | Controlled native disappearance tests plus eight native builds with the corrected Worker collector running concurrently (370 passes, 17 names removed, no errors). The zero-size target was confined to disposable test roots. |
| Abandoned temporary files do not accumulate by default | Dead-owner staging is removed even with retention settings unset; live and malformed owners remain untouched. |
| Removing staging aliases preserves accounting and complete winners | Three regressions cover a killed publisher's remaining staging link, a staging name whose only other alias is outside the managed store, and the retained-size calculation after alias removal. All fail against the preceding collector and pass with the fix. Removing an extra name does not report its still-linked payload as reclaimed blocks. |
| Clearing the disposable root needs no manual repair | Recognized native layout is adopted after recreation; unknown/foreign layouts remain untouched. |
| Isolation and source preservation | Verification and recovery warming select their existing private path; Bubble strips the opt-in; trusted Mathlib publication remains separate. Recovery source/Git data is outside this collector. Snapshot copying does not certify CI provenance. |
| Private fallback and rollback | Real native private-rollback/old-child tests and router missing/broken/changed-registration tests pass. Rollback does not delete shared data. |
| Live rollout preserves useful work | All manager handovers preserved the 40 independent worker scopes. All 40 enabled workers now run the cache integration; a separate process audit found exactly 40 loops, no duplicate or unexpected IDs, matching configurations, and the original state-lock inodes still open. Worker replacements used idle/backoff boundaries with no round children. |

Both native branches pass the 20 real shared-cache regressions and standalone
Lake builds. The upstream [Linux CI run](https://github.com/leanprover/lean4/actions/runs/42169213507)
passed its full build/test suite, including 4,831 tests. Worker validation includes
29 machine-cache tests and 62 retention tests. All nine jobs in the complete
[Worker CI run](https://github.com/TauCetiProject/TauCetiWorker/actions/runs/42177480442)
passed at revision `afaf5163ad56ee200664fa73bcfa5cb0f78d94f9`.
The production pin passes
[Fleet PR CI](https://github.com/TauCetiProject/TauCetiFleet/actions/runs/42175762332).

Focused Worker validation also covers retention, expanded caches, host warming,
Mathlib, verification, recovery and native filesystem behavior. A local full
portable run exposed an unrelated saved-production-schema failure in queue
feeds; it reproduces on the untouched base revision. The CI portable suite passes.

## Measured reuse

A small two-workspace build on tauchonki took 0.779 seconds initially and 0.358
seconds in the second workspace. The second build performed no module
compilation. Both `Example.olean` paths have inode `5508880673`; the cache and
both outputs are aliases of one complete artifact. This fixture requires no
artifact downloads and measures local reuse, not production network savings.

A repeat used distinct worker-private fallback paths in the two workspaces.
The first build took 5.575 seconds under host pressure; the second took 0.328
seconds without compilation. Both workspaces shared the public, server and
private artifact inodes, and evaluating the exported definition returned the
expected value in both. These timings are small-fixture observations, not an
estimate for an entire worker checkout.

A separate local HTTP fixture transferred one 4,096-byte artifact on the cold
native batch fetch (0.416 seconds), then made zero HTTP requests and transferred
zero artifact bytes on the warm fetch (0.268 seconds). This isolates download
reuse; it does not estimate fleet-wide network savings.

Fresh production preparation in workers 3 and 5 took 49 seconds from the warming
message through the main-cache-ready message. Neither ran the private expander.
Other workers took longer under concurrent Mathlib preparation and disk pressure;
these are observations on different revisions, not a controlled before/after
benchmark. Updated workers 6, 12, 18, 22, 23, 26, 32 and 39 posted reviews during
the rollout.

Sampling real worker checkouts found ten shared module artifacts after checking
14 module paths. `TauCeti.olean` has inode `6599607124` in workers 9, 14, 17 and
25 and in the native store. `TauCeti/Data/Fin/Basic.olean` has inode `8347047923`
in workers 9, 17 and 25 and the store. These observations demonstrate actual
cross-worker reuse; they do not establish host-wide physical savings.

The corrected live collector reported 24,772,435,968 retained allocated bytes, of
which 24,133,988,352 bytes had additional links. Both retention targets were
unset and no collection errors were recorded. It removed four abandoned
temporary names and reported zero estimated payload blocks reclaimed. The store
is growing as more workers adopt it. A subsequent dry-run inventory at 07:19 UTC,
after all 40 workers had adopted the integration, reported 24,992,493,568 retained
bytes, including 24,045,010,944 bytes with additional links, with both targets
still unset and no errors. These are inode-deduplicated inventory estimates, not newly
allocated bytes or bytes saved. XFS reflinks, existing CI aliases and concurrent
writes prevent those interpretations.

## Other measured storage

The existing current-worker collector inventoried 520,146,571,264 allocated bytes
across the private caches, current workspace build trees and existing publication
pool. Of these, 460,229,029,888 bytes were protected by live workspaces and
38,390,059,008 bytes had links outside that inventory. This is a long-running,
inode-deduplicated estimate during live work, not an instantaneous physical total.
It overlaps the new native store where workspace aliases refer to native payloads.
The sweep completed successfully with no errors; it did not discard unfinished
source. Its observed RAM usage reached 4.8 GB, so repeating this broad legacy
sweep during host memory pressure is inappropriate.

One retained Mathlib checkout still contains a `build-before-sharing` tree with
7,218,102,272 allocated bytes alongside 5,826,269,184 bytes in its current build
tree. This is one concrete example, not an estimate for every retained checkout.
The visible allocation estimate for that `.lake` subtree is 13,044,375,552 bytes;
kernel-owned overlay work areas could not be traversed with the available
account. Reflink sharing can also make the physical cost smaller than these
inode-based estimates. Existing retained build trees are outside the new native
collector, and their source-preservation rules remain in force.
Excluding `.lake`, the same Mathlib checkout has 172,437,504 allocated bytes,
including 45,948,928 bytes of Git data. Source/Git sharing and removal of retained
build generations remain separate from native artifact-cache selection.

A separate scan measured 118,520,356,864 allocated bytes under `/tmp`, including
98,777,571,328 bytes in 184 `tauceti-review-*` scratch directories. Another
3,197,337,600 bytes were under Nix shell/development directories. Scratch and
retained source trees were not bulk-deleted to satisfy a cache target.

QEMU's disk information showed three busy CI overlays at approximately 31.6,
32.7 and 32.3 GB allocated, and the idle overlay at 89 MB. Each has a 64 GiB
virtual capacity and references the same prepared base image. The image directory
accounts for another 4.08 GB. These are observed per-runner costs that vary with
job activity, rather than preallocated 64 GiB per runner.

A host snapshot at 07:19 UTC measured 3,691,381,080,064 bytes used of
3,837,570,154,496 bytes, with 146,189,074,432 bytes available and 119,238,533
inodes in use. Free space fluctuates as active work and existing
cleanup proceed. The overall scan of retained roots is still pending, so these
component inventories do not yet close the host-wide accounting. They must not
be summed as disjoint physical allocations.

The existing Fleet memory-pressure guard temporarily held new rounds during
validation. Configuration still has workers enabled, `draining = false`, and
40 science slots. The deployment did not change that guard or its thresholds.

## Current rollout

The boundary rollout is complete across the 40 enabled science workers. The
manager and background collector now use worker revision `cbf2b2da4c2359603bb69eea4064e670c4213cee`;
existing worker loops retain compatible earlier revisions with identical
per-round cache integration. All 40 scopes survived the final manager handover,
and dependency versions match the previous production runtime. The 16 configured
subscription slots remain explicitly stopped. Host and worker configuration
hashes are unchanged by the rollout.

A fresh independent audit at 07:29 UTC again found exactly 40 compatible worker
loops, no duplicate or unexpected worker IDs, matching specifications, and every
original instance-lock inode still open. The original stock Lake signature and
installed native executable hash match. Normal worker and CI admissions remain
configured as enabled; the existing memory-pressure guard can temporarily hold
new rounds without a deployment drain. The subscription pool remains inactive.

The retained-data scan is the only outstanding measurement. Representative
private-cache, dependency-source, retained-build, download and build observations
are recorded above. Disk pressure arose during rollout: host free space fell
below 30 GB while the shared store was below 10 GB. That observation alone does
not identify the other growth or prove disk relief from this feature.

Dependency source trees, Git checkouts, recovery snapshots and existing private
aliases remain substantial separate costs. This implementation does not provide
whole-workspace overlays or bulk-convert those trees. Measure those costs before
extending the design.

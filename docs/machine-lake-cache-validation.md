# Native Lake cache implementation validation

Evidence recorded on tauchonki on 6 November 2026. Deployment is in progress;
the final fleet-wide accounting and completion check remain open.

## Delivered changes

- [Worker integration](https://github.com/TauCetiProject/TauCetiWorker/pull/573):
  per-invocation native selection, private fallback, runtime installation/router,
  isolation at existing verification boundaries, and optional background cleanup.
- [Native Lake fixes](https://github.com/leanprover/lean4/pull/16301), with a
  [v4.36.0-rc2 backport](https://github.com/kim-em/lean4/commit/2fab607f76571f4f399e2332e69d674b98277d64).
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
| Collection races do not become work failures | Controlled native disappearance tests plus eight native builds with the real Worker collector running concurrently (530 passes, 17 names removed, no errors). The zero-size target was confined to disposable test roots. |
| Abandoned temporary files do not accumulate by default | Dead-owner staging is removed even with retention settings unset; live and malformed owners remain untouched. |
| Clearing the disposable root needs no manual repair | Recognized native layout is adopted after recreation; unknown/foreign layouts remain untouched. |
| Isolation and source preservation | Verification and recovery warming select their existing private path; Bubble strips the opt-in; trusted Mathlib publication remains separate. Recovery source/Git data is outside this collector. Snapshot copying does not certify CI provenance. |
| Private fallback and rollback | Real native private-rollback/old-child tests and router missing/broken/changed-registration tests pass. Rollback does not delete shared data. |
| Live rollout preserves useful work | Both manager replacements preserved all 40 independent worker scopes. Only idle/backoff boundaries with no round children permit worker restarts. Ongoing paid rounds are left running. |

Both native branches pass the 20 real shared-cache regressions and standalone
Lake builds. The upstream [Linux CI run](https://github.com/leanprover/lean4/actions/runs/42165034905)
passed its full build/test suite, including 4,831 tests. The Worker integration's
functional [CI run](https://github.com/TauCetiProject/TauCetiWorker/actions/runs/42160999154)
passed. The final extensionless-executable cleanup change adds a regression,
passes all 26 machine-cache tests locally, and passes the complete
[Worker CI run](https://github.com/TauCetiProject/TauCetiWorker/actions/runs/42167078533).
The additional native race tests are also passing locally; their new upstream CI
run is pending. They do not change the installed native executable.

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

The latest native-store inventory reported 19,701,616,640 allocated bytes, of
which 19,382,083,584 bytes had additional links. Both retention targets were
unset, no files were removed and no collection errors were recorded. The store
is growing as more workers adopt it. These are inode-deduplicated inventory estimates, not newly
allocated bytes or bytes saved. XFS reflinks, existing CI aliases and concurrent
writes prevent those interpretations.

## Remaining validation

Finish the boundary rollout across the 40 enabled science workers and observe
useful completions. The 16 configured subscription slots are explicitly stopped
and remain stopped. No account or worker-count changes are part of this rollout.

Record final cache inventory, host allocated blocks/inodes, remaining private and
retained-workspace storage, and representative preparation/download/build
measurements. Disk pressure arose during rollout: host free space fell below
30 GB while the shared store was below 10 GB. Existing cleanup is running under
existing policy. That observation alone does not identify the other growth or
prove disk relief from this feature.

Dependency source trees, Git checkouts, recovery snapshots and existing private
aliases remain substantial separate costs. This implementation does not provide
whole-workspace overlays or bulk-convert those trees. Measure those costs before
extending the design.

# Machine-wide Lake artifact cache

Use Lake's native local artifact cache across worker workspaces. Fix concurrent cache I/O, enable sharing where supported, and add optional age/size cleanup. Reuse native hashes, mappings, invalidation and artifact restoration instead of implementing another build cache.

This is an implementation plan, not a production change. Worker counts, admissions, accounts, spending and task deadlines are outside its scope. New storage limits require an explicit user/operator setting. Cache failure must fall back to existing private/refetch/rebuild behavior rather than introducing a new reason to stop useful work.

## Starting point and scope

TauCeti enables `LAKE_ARTIFACT_CACHE=1` and `LAKE_RESTORE_ARTIFACTS=1`, but `agents.share_build_caches()` sets a separate `LAKE_CACHE_DIR` for every worker. The restore setting puts artifacts in conventional workspace locations, normally through hard links. Some CI artifacts are already pooled by `expanded_cache.py`, so separate pathnames do not imply independent physical storage. Locally produced artifacts and retained build generations still offer substantial sharing opportunities. See the [native Lake documentation](https://lean-lang.org/doc/reference/latest/Build-Tools-and-Distribution/Lake/).

The inspected Lake v4.36.0-rc2 has several unsafe write paths for a common writable store. Recheck every production toolchain before implementation:

| Native path | Concurrency problem |
| --- | --- |
| Batch download: `transferArtifacts` / `monitorTransfer` | Writers share `<artifact>.tmp`, delete it, validate by pathname, and rename by pathname. Another writer can replace the validated temporary file before publication. |
| Lazy build-time fetch: `resolveArtifact` / `downloadArtifactCore` | Downloads delete/write the final artifact pathname directly. Another worker can restore and trust a partial file. |
| Additional download destinations: `extraPaths` / `createExtraPaths` | Copies write directly to final pathnames. |
| `saveArtifact` and its copy/text fallbacks | Exclusive creation of a final pathname followed by writing exposes partial bytes to readers. |
| `writeOutputsCore` / `downloadRevisionOutputs?` | Direct metadata writes expose incomplete JSON/JSONL, including revision files subsequently trusted merely because they exist. |
| Restore/archive unpack overlapping GC | An artifact can disappear after lookup and before hard-linking or unpacking. That must become a recoverable cache miss. |

These observations are from the pinned [cache I/O implementation](https://github.com/leanprover/lean4/blob/v4.36.0-rc2/src/lake/Lake/Config/Cache.lean), [artifact save/restore implementation](https://github.com/leanprover/lean4/blob/v4.36.0-rc2/src/lake/Lake/Build/Common.lean), and the download/module helpers they call. Fix both explicit downloads and lazy fetches; wrapping only `lake cache get` misses build-time accesses.

The shared local store is one cooperating host-user domain: workers may reuse each other's native local build outputs. It cannot simultaneously promise that all its contents came from trusted CI. Keep it separate from the existing CI-only publication pool. Verification that requires CI-trusted or independently rebuilt outputs must use its existing isolated path, not silently acquire the shared local mappings. Preserve Bubble's existing cache isolation; do not mount the host store writable into containers. Shared hard links are not a security boundary: a same-user process that deliberately changes permissions and writes an artifact in place can affect other workers. Preserve stronger existing isolation; use private copies/reflinks for mutable data rather than claiming read-only modes provide copy-on-write.

Source files, credentials, Git state and recovery records remain outside this disposable cache. Dependency source sharing and whole-workspace overlays are subsequent work, justified by the remaining measured duplication.

## 1. Fix native publication and cache-miss handling

Implement the smallest changes in Lake, preferably upstream, with a narrowly reviewed backport only if needed. Do not silently replace the Lean compiler or change project toolchain pins to deliver cache sharing.

Use one publication protocol for all the affected paths:

1. Write/download to an invocation-unique temporary file on the destination filesystem. Validate downloaded content using Lake's existing checks. Set the intended payload permissions before publication.
2. Publish complete artifacts atomically without replacing a completed winner, for example by hard-linking the complete temporary file to the final content-addressed name. On `EEXIST`, use the completed winner and discard only this invocation's temporary file. Never expose a final name while bytes are still being written, and never remove another invocation's temporary file.
3. Write complete output/revision metadata to unique temporary files and rename atomically, preserving native overwrite/no-overwrite semantics. The inspected store uses whole files per input hash or revision; it does not need a general merge-lock manager or transactional database. Keep native scope rules and validation.
4. Treat disappearance during resolve, restore, or archive unpack as a cache miss. Continue through native refetch/rebuild, or use a private cache for the affected operation if concurrent eviction keeps interfering. This must also work after an operator clears the disposable shared cache. Do not turn an optional cache problem into a new module-build failure, persistent stop flag, or manual repair prerequisite.
5. Ensure native rebuilds replace/unlink restored outputs before writing. In-place writes through a restored hard link would corrupt every alias. Keep any path that legitimately needs in-place mutation private or use a private copy/reflink. Read-only modes alone do not isolate same-user agents that can change permissions.

Use ordinary unlink for obsolete files; it is atomic. Unique staging can be cleaned after its owner exits. Reuse existing process ownership checks instead of a new stale-lock protocol. Do not hold a host-wide lock across downloads, extraction, builds or paid rounds. Duplicate transient downloads are acceptable; only completed data becomes visible.

## 2. Select shared storage safely for each Lake invocation

Cache selection must follow the actual executable/toolchain each time Lake runs. A worker can switch toolchains after checkout or rebase, and agents can invoke Lake directly. A once-per-worker capability check or a PATH shim alone cannot prevent an older executable from writing into a shared root.

Provide a native opt-in selection mechanism in the fixed Lake. It must coexist with the existing private `LAKE_CACHE_DIR` fallback: an older Lake ignores the new opt-in and keeps its private path; a fixed Lake selects shared storage only when its prerequisites hold. Define and test this interface before changing worker defaults, including Lake's exported child environment, nested invocations, direct toolchain-binary calls and an old/new/old toolchain transition. Use existing upstream support if equivalent behavior is available; a new environment variable is a proposed interface, not an existing Lake feature.

Start with managed cache directories separated by resolved toolchain identity on the checkout filesystem. Native traces already encode Lean identity, but the inspected local revision-file paths lack toolchain scope. Do not merge those revision namespaces without fixing and testing that behavior. Linked/dirty toolchains need a reliable distinct identity or retain private mode. Honor explicit user cache overrides.

Shared mode requires the following capabilities; an unmet condition retains ordinary private behavior rather than rejecting the user's work:

- The running Lake has the complete publication and disappearance fixes.
- `LAKE_RESTORE_ARTIFACTS=1` remains effective for the outputs consumed by the operation. An explicit request for non-restored cache paths keeps that operation private unless its reader lifetime is independently supported.
- Workspace and store are on the same filesystem, so restored hard links provide sharing. Verify this from filesystem identity, not path spelling.
- The invocation belongs to the cooperating local-cache domain, with existing isolation preserved elsewhere.

In compatible shared mode, bypass `expanded_cache.preparation()` rather than pointing its current private transfer/corruption-recovery operations at the shared directory. Native Lake already unpacks archives and stores individual outputs. Keep the existing expanded-cache path for private/unsupported mode. Retain the existing restore setting until native tests cover TauCeti's audits and verification tools.

Keep Mathlib's `lake exe cache get` separate from Lake's `lake cache get`. A Lake fix does not make an older Mathlib downloader concurrency-safe; preserve its existing completed-file pooling or isolated immutable view.

Allow the shared store to populate through normal native builds and downloads. Native Lake can cache an up-to-date local build when its shared entry is missing. Demonstrate that behavior with production-shaped fixtures rather than adding a fleet-wide import/copy job. Concurrent cold requests can perform redundant downloads safely; measure this before adding any scheduling machinery.

Leave the existing per-worker toolchain-bump cleaner scoped to private stores. Never point its whole-directory deletion at the shared root. Old private caches retire through their existing safe cleanup path; do not rename, relink or delete live workspace trees as part of the configuration switch. Existing XFS block deduplication can remain complementary maintenance for old duplicates.

## 3. Add optional selective background cleanup

Provide optional operator settings for maximum retained age and a retained-size target. Either may be unset. Apply selected settings across the managed toolchain directories, rather than giving every directory an independent copy of the host budget. Document whether archives, extracted artifacts, metadata and staging count toward that target.

No new numeric default is selected by this plan. In particular, do not inherit `cache_storage.limits()`'s implicit 25%-of-disk cap, the private 72-hour generation lifetime, or its worker-flush requests merely by including the shared root in the old collector. Existing production policy is unchanged. With neither option selected, the new collector does not age/size-evict the shared store; report its growth and retain the host's existing disk-pressure behavior. No configuration can promise unlimited retention and a disk that never fills. Configure the desired retention values explicitly when enabling cleanup; do not invent them in the implementation.

Use the smallest collector that is safe with native missing-artifact recovery:

- Extend the existing background cleanup entry point and dry-run reporting. Keep full scans out of status rendering and paid-round startup.
- Consider completed artifact files with one remaining hard link first. A name with workspace aliases cannot release those aliases' storage; leave active/pinned material alone and report it. Check file identity and type without following symlinks.
- Use filesystem change time as an inexpensive approximation of recent link activity, not exact last use or creation age. Recheck the installed native behavior: not every read creates a new link. Start without a new access journal or per-artifact lease manager. Define the chosen age semantics in the user documentation.
- Reclaim eligible unlinked-from-workspace payloads in oldest-first order when the requested age or size target calls for it. Also expire eligible metadata using the requested age; dangling mappings are ordinary native misses. Clean abandoned staging only after its owner is known to have exited.
- A link-count check can race a new reader, and archives may be unpacked directly by pathname. Safety therefore depends on the native disappearance fixes and their tests, not on link count or a grace period proving that there are no readers. Do not add an arbitrary time floor as a substitute for race handling.
- Cache writes, staging and active workspace links can temporarily exceed a reclamation target. Report the excess and its cause; do not block work, terminate rounds, or flush another worker to enforce the number.
- Preserve the existing cleanup rules for source-bearing retained/recovery checkouts. Their artifact links may keep blocks alive after cache cleanup. Do not discard unfinished source or Git history to meet the cache target.

Count hard-linked payloads once across the inventoried roots. Report cache entries removed, estimated retained bytes, bytes with additional links, and observed host free space separately. XFS reflinks can share extents between distinct inodes, so `st_blocks` and summed directory sizes cannot establish exact physical savings. Keep accounting approximate where necessary and label it.

## Validation

Use real native Lake processes and small representative projects. Extend existing integration fixtures rather than testing only environment settings. Synchronize a local HTTP server to pause partway through a download and deliberately overlap readers/publishers. Exercise restore/unpack disappearance with a controlled test hook or equivalent interposition.

| Scenario | Required behavior |
| --- | --- |
| Identical module built in two workspaces; edit and switch back | Native reuse and invalidation remain correct; payload sharing is observable. |
| Overlapping batch downloads, lazy fetches, and extra destinations | Unique staging; final names expose only complete verified payloads. |
| Slow local artifact creation with another reader | Reader sees completed bytes or a recoverable miss. |
| Concurrent output/revision publication | Whole parseable files with native replacement semantics. |
| Publisher killed before or after publication | Complete winners survive; abandoned staging can be removed safely. |
| GC deletes a candidate between lookup and restore/unpack | Refetch, rebuild or private fallback succeeds without a new build failure. |
| GC or an operator clears the shared store during a build | Useful work completes through the tested cache-miss path; no persistent latch. |
| Supported native build/rebuild tools replace a restored output | Other workers' completed artifacts remain unchanged; tools that require in-place mutation stay private. |
| Round changes between fixed and unfixed Lake versions | Older Lake never writes into the shared root, including direct/nested calls. |
| Toolchain, dependency, platform or build settings differ | Native outputs and revision metadata remain correctly scoped. |
| Cache unavailable, unsuitable filesystem, or explicit private override | Existing private operation remains usable. |
| Size/age options unset, individually set, or changed | Only requested policies apply; target excess never stops work. |
| Cache target encounters workspace/recovery links | Accounting reports pinned space; source-preservation rules hold. |

Verify host work, isolated verification, recovery and Bubble separately. Test cache publication while collection runs and demonstrate that existing workspace outputs seed a cold shared store. Confirm that the existing private cache and trusted-CI path still work.

## Rollout and completion

Deliver native I/O fixes, per-invocation selection, and optional cleanup as separate reviewable changes. A production toolchain change must be explicit. Configure the intended cleanup policy visibly, then switch existing workers at natural round boundaries while normal worker and CI admissions remain enabled. Inspect useful completions and failures before extending to the rest of the fleet.

Rollback selects private storage for subsequent invocations without deleting the shared pool or interrupting its existing users. Keep unsupported toolchains on the existing path. No bulk migration, fleet drain, new daemon or persistent readiness database is needed.

Measure host allocated blocks and inode usage, inode-deduplicated cache size, remaining private artifacts, downloaded bytes, preparation time and first-build time. Do not claim all dependency/recovery duplication is removed. [Existing XFS deduplication work](https://github.com/TauCetiProject/TauCetiWorker/pull/566) can help old copies but does not establish native-cache correctness or exact reclaimed bytes.

Completion means compatible invocations share native artifacts; publication and eviction races recover without corrupting or stranding work; only requested retention targets apply; private fallback and rollback remain usable; and actual storage/build measurements show the benefit. Document remaining dependency source and retained-workspace costs before extending the design to whole-workspace layering.

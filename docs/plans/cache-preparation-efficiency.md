# Faster cache preparation

Reduce repeated cache work while preserving native Lake validation, shared-artifact correctness, private downloads, and automatic fallback. Keep preparation state disposable and avoid new dependencies on bookkeeping for useful work.

## Evidence and scope

Across 126 observed preparations, cache-warming start to agent launch had a 51-second median and 129-second 90th percentile. That whole-phase result does not establish which substep dominates.

`expanded_cache.preparation()` currently links every pooled/private compressed archive into a fresh directory before native Lake selects the required outputs. Transfer, expansion, and hydration repeatedly discover or decode mappings. Mathlib also validates its extracted inventory on reuse. Optimize measured repeated work in `expanded_cache.py`, `agents.py`, and related helpers; keep resource telemetry in the [separate cgroup plan](cgroup-resource-accounting.md).

## Implementation order

1. **Attribute preparation cost first.** Add cheap elapsed-time and item-count fields to the existing preparation receipt for seeding, native fetches, expansion, transfer/hydration, and Mathlib preparation. Separate CPU from elapsed time where available. Compare representative warm rounds and a small cold-cache fixture. No sampling service or prolonged benchmark campaign is needed. Choose changes using total preparation and first-build cost, including bandwidth and recompilation.

2. **Remove duplicate work within a preparation.** Parse the fresh native-download mappings once and carry the resulting input-to-archive mapping and referenced-file sets through transfer, expansion, and hydration. Group by archive once, reuse decoded entry metadata within the operation, and avoid repeated checks of the same file. Keep current checks for private mapping contents, artifact presence, and safe paths; write only missing or changed data. A subsequent round must still detect missing or altered private files. Add no cross-round readiness receipts, prepared-set index, or cache-state database.

3. **Reduce archive seeding if its measured cost is material.** Keep the existing single `lake cache get` invocation for each branch/main lookup. Use the pooled revision maps already copied into that preparation as hints for which completed pool archives to link, while retaining reuse of the current worker's completed private archives. Parse those hints only once; use them to reduce pool file operations, not to decide the authoritative selected revision or scope. Native Lake still performs selection, downloads missing artifacts, and supplies the trusted output mappings used for publication.

   An absent, stale, malformed, or incomplete hint merely seeds fewer files. Treat an archive disappearing between lookup and link as a cache miss for that file; continue seeding the others and let Lake download it. Keep existing partial downloads private. A bad hint must neither select the wrong outputs nor abandon sharing for the entire round.

   Measure the hint-parsing cost and any extra downloads against the saved filesystem work before adopting this change. Do not add a mappings-only prepass, another workspace load, custom revision selection, or a persistent scoped store. Preserve existing CLI arguments, compatibility, and classification of workspace errors, cache misses, and transport failures. [Pinned native fetch implementation](https://github.com/leanprover/lean4/blob/v4.36.0-rc1/src/lake/Lake/CLI/Main.lean#L502-L632).

4. **Keep Mathlib cache identity and inventory stable.** Leave `inventory()`, its serialized format, comparison, and validation frequency unchanged in this work. Existing ready markers must remain valid after deployment. An inventory-format mismatch currently reaches an integrity-failure path that disables sharing across the pool, so this is not an appropriate place for a speculative traversal rewrite or cached validation result.

   If measurement justifies it, combine the Lean version/prefix queries into one Lake environment load. Preserve dependency synchronization and produce exactly the same `signature()` specification and key, including ABI text, resolved paths, and all existing context. Check equality before shipping; an incidental key change would force unnecessary snapshot republishing. No new cache format or toolchain requirement is needed.

5. **Preserve correctness and automatic recovery.** Only fresh trusted CI mappings may publish shared entries; mutable private mappings and locally compiled outputs remain private. Keep read-only shared payloads, atomic completed-entry publication, existing leases, nonblocking contention, and private copy-up. Retain current refetch/private-restoration behavior for missing or corrupt artifacts and interrupted preparation. Preserve useful completed downloads and populated pools. Add no global lock, wait condition, admission refusal, retry ceiling, hard-failure timeout, persistent stop condition, or manual repair requirement for the optimization. Existing integrity checks remain unchanged.

## Validation and rollout

Extend existing small fixtures only where behavior changes. Verify selective seeding leaves an unrelated pool archive out of the download/private directories, and that stale hints or concurrent archive removal still allow the normal fetch. Retain existing corrupt-archive refetch, private-write isolation, trusted-publication, and native source-rebuild tests. Keep hydration coverage for deleted/altered private state. If combining toolchain queries, add a focused check that the specification and key are unchanged.

Use the existing native Lean fixture to confirm edited source rebuilds and another worker's outputs remain unaffected. Compare representative preparation plus first-build time, downloaded bytes, and CPU/I/O before and after. Do not introduce a large benchmark matrix or tests counting internal JSON reads.

Ship the measured improvements through the existing worker/runtime pin flow, preserving the populated pool and normal scheduling. Completion means materially cheaper preparation with correct builds and automatic fallback; it does not require every possible optimization. Report remaining Mathlib inventory cost separately rather than weakening its checks or claiming all work now scales only with changed artifacts.

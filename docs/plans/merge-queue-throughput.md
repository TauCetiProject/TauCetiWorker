# Merge queue throughput plan

2026-11-10. Scope: improvements 1–5 from the review of batches 393–399.

Keep useful landings ahead of accepted arrivals, with automatic recovery from temporary failures. Reuse existing ownership, checkpoints, retained refs, caches and publication records. Preserve candidate identity, attribution, required checks and promotion rules; use stock Lean/Lake. Add no service, coordination protocol, cache database or recurring operator procedure. Item 6, control-plane release separation, is outside this plan.

## Work

1. **Simplify the cache path around native Lake.** Generate Lake's `outputs.jsonl` during the final build (`lake build --iofail -o ...` with artifact caching enabled), stage with `lake cache stage`, and upload from the isolated publisher with `lake cache put-staged` and explicit revision/repository/toolchain. Lake owns input validation, download hash checks and artifacts-before-map upload ordering. Keep its native map; remove our additional `publication.json`, omission manifests, HEAD/ETag probes, inherited-object re-downloads, custom upload planner and manual full-publication recovery switch. Accept full artifact uploads initially and measure total time/bytes. Keep minimal checks on incoming file paths/types, credential isolation and exact-revision publication evidence. Avoid a second whole-build scan merely to produce the map; preserve necessary rehashing where candidate-written hash sidecars are untrusted. Verify native archive reuse before removing the old reconnect workaround; retain bulk restoration only if a benchmark demonstrates a material benefit. Existing early publication can overlap checks over stable outputs; CI setup starts immediately, with no new readiness gate. Cache failures remain visible and retryable.

2. **Prepare the successor on its intended parent and consolidate rebase checks.** When that parent is available, schedule future preparation directly on it without requiring a full old-base build first. Preserve prior repairs, membership, adoption guards and one writer per batch. Keep focused proof checks during conflict resolution; check the combined downstream closure after rebasing. Extend worker-owned final verification to rebases. Use 398's retained case—98 minutes in two intermediate dependent builds—to verify equivalent final checks with less repeated work. Leave active tasks undisturbed.

3. **Make worker verification match staging.** Use staging's pinned tools, scope-selection rules and conservative fallbacks, including full lint when staging selects it. Worker lint covered 426 modules in 398 and missed a staging finding. Return findings through the existing repair loop. Worker verification remains preparation evidence; staging supplies the landing verdict.

4. **Overlap measured independent checks.** Full lint took 15–20 minutes. Measure import/docscan and individual-linter costs before optimizing; the driver is already compiled. Overlap audits and lint after confirming output stability and peak memory. Collect every failure separately. Keep children within the existing task lifecycle and join/cancel them before cleanup. Retain serial execution if overlap is unsafe or slower. Defer early public-cache upload changes until this simpler overlap demonstrates a gain.

5. **Resume successful work after transport failures.** Retain verified tips and exact source/checking identities in existing checkpoints and refs. Retry push/submission without repeating verification while that identity remains current; reconcile ambiguous responses and reject stale adoption. Check Git submission capability before expensive work. Bound individual attempts while retaining pending work for automatic recovery. Shorten missing-run waits only after duplicate deliveries coalesce without cancelling healthy runs. Use existing durable intents for noncritical status retries outside the build path. Remove terminal worktrees as before.

## Delivery and acceptance

Establish verification parity and recovery first (3, 5), then improve cache handoff and successor preparation (1, 2), then enable measured overlap (4). Ship small changes independently through the existing deployment path, with admissions enabled. Account explicitly for automatic supersession when a deployment advances `main`.

Extend existing fixtures for native stage/upload/restore, changed-source rebuilds, interrupted uploads, missing/corrupt caches, hostile staging paths, lost push responses, restart, supersession and concurrent-check failure. Recovery must preserve useful work and resume automatically. Compare historical cases and natural live batches using preparation-to-landing time, transferred bytes, fresh builds/restorations, phase times, retries, useful landings, arrivals and backlog age. Avoid double-counting overlap. Revert regressions through normal deployment; completion requires faster progress and reliable autonomous recovery.

## Implementation notes

Native upload/restore integration covers failed artifact uploads (no revision map), retry from the
same staged data, workspace-free publication, archive reuse, changed-source rebuilds and corrupt
download rejection. The old bulk-unpack shortcut was removed: examined production runs fell back
to ordinary Lake restoration, and the native fixture confirms restored archive reuse.

Overlap requires matching measured audit/lint peaks and headroom; initial and uncertain runs stay
serial. Per-linter profiling is optional because running linters separately changes batching cost.
Legacy dispatch grace stays unchanged until duplicate deliveries demonstrably coalesce safely.

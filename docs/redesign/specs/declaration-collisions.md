# Declaration collisions before assembly

Detect duplicate declarations between batch-mates when a batch is assembled, from compiled module headers recorded at PR build time, so that a collision costs seconds at construction instead of a full staging build per collision. The post-build audit in `scripts/Axioms.lean` stays the authority; this check only prevents known collisions from entering a candidate.

## Problem

Two PRs in one batch can each declare a constant with the same fully qualified name in different modules. Each compiles alone and both compile together, so `lake build` passes; only the audit step, which loads the whole built environment and requires every TauCeti-owned name to be defined in exactly one module, fails the candidate. On 2026-11-04 the 2598 lineage spent three consecutive rounds on this (candidates 1949, 1950 and 1951: `Orthogonal/Commutant.lean` versus `Orthogonal/TwoStrands.lean`, `Identity.lean` versus `Curvature.lean`, `Morse/Cell/Band.lean` versus `Morse/Deformation/Homology.lean`), about 40 minutes of the serial staging slot each, after a clean compile every time. Removing `--fail-fast` cannot collapse these rounds because the compile never sees them.

Reading declaration names from Lean source text is not reliable (namespaces, sections, `variable`, attributes, macros). The compiled module header is: `env.header.moduleData[i].constNames` is exactly what the audit compares, it is available without elaboration, and reading it takes seconds.

## Design

### Record names at PR build time

Every PR already receives a trusted `pr-build` run that compiles its modules against `main` and runs the audits. After the build, a new workflow-pinned Lean script, `scripts/DeclarationNames.lean` (human-owned, same shape as `DuplicateDeclarations.check` in `scripts/Axioms.lean`), reads the headers of the modules the PR adds or modifies (the changed-module list the lint scope already computes) and writes one JSON document:

```json
{"version": 1, "head": "<PR head SHA>", "base": "<merge base SHA>",
 "modules": {"TauCeti.Geometry.Manifold.Morse.Cell.Band": ["TauCeti.isZero_singularHomology_sublevelPair_of_mfderiv_ne_zero", "..."]},
 "complete": true}
```

Names are the module's constant names with the audit's exemption applied unchanged: a reserved auxiliary name (`isReservedName`) is excluded unless some module supplies a declaration range for it. Names are sorted; modules the PR deletes are absent. Bounds: 4,096 modules, 65,536 names per module, 8 MiB per document; a document over any bound is written with `"complete": false` and an empty `modules` map. The script reads `.olean` headers only; it never executes candidate code or elaborates anything.

The workflow uploads the document as the artifact `declaration-names-<head SHA>` with 30-day retention and prints one log line, `DECLARATION-NAMES {"head": "<SHA>", "modules": N, "names": M, "sha256": "<digest of the document>"}`, so that the record can be bound to the exact head and run the way `LINT-ENV-LOCATIONS` is today.

### Bors records the set at admission

When bors admits a head (the admission snapshot that already stores the exact head, changed files and labels), it fetches the artifact for that head from the run that produced the `build` status, verifies the head and the digest from the log line, and stores the module-name map with the admission. A missing, expired, mismatched, incomplete or malformed document stores `names: unknown` for that head. A changed head gets a new build and a new record; an old record is never carried to a new head.

Bors also keeps, for every PR landed by promotion, that PR's recorded map together with the landing time, for 30 days.

### Check at assembly

The planning pass that already tests each unit for merge conflicts against the base plus the already accepted units (`merge_selection`) gains one more test per unit, run before the Git replay since it is cheaper:

1. Against accepted units: if any name in the unit's map is also in an accepted unit's map, and the two modules differ, the unit collides.
2. Against recently landed PRs: if any name in the unit's map is also in the map of a PR landed after the unit's PR build (by the recorded landing time against the build's run time), and the modules differ, the unit collides. Collisions with names that were already on `main` when the PR was built were caught by that PR build's own audit and need no index of `main`.

A colliding unit is deferred exactly as a merge-conflicting unit is deferred today, with a request that names the colliding constant, both modules and the other PR (for case 1) or the landed commit (for case 2); the batch is assembled without it. Same-module matches are not collisions: two PRs editing the same module with the same name are a merge conflict or a no-op, and both are handled elsewhere. A unit whose map is `unknown` is not checked; a unit with a known map is checked only against other known maps. Bundles are checked as one unit.

No new state machine: deferral, the request, readmission after a new head, and the audit's verdict are the existing paths. The check is a pure function of recorded maps and runs under the planning pass's existing sixty-second deadline; if it does not finish, the pass proceeds without it and logs that the check was skipped.

### Settings and observability

One runtime switch, `BORS_DECLARATION_COLLISIONS` (`off` by default until the producer has been live for a day; then `on`), read at each planning pass. The candidate journal records a `declaration_collision` event per deferral with the name, modules and PRs, and the admission snapshot records whether the map was present, unknown or incomplete. The daily journal report counts: deferrals by collision, audit collisions that still reached a candidate (expected to fall to the unknown-map cases), and PRs admitted with unknown maps.

## Failure modes considered

- **Missing or stale record.** Unknown maps are not checked; the audit still catches the collision one build later, as today. The check can only remove rounds, never add a false green.
- **False deferral.** Only exact name equality in different modules counts, with the audit's own exemption for reserved auxiliaries, so a deferral is a collision the audit would have reported. The request quotes the name and both modules, so a human can see the reason at once.
- **Auxiliary names that renumber between compilations.** Excluded by the exemption, as in the audit; a user-written `.eq_1` with a declaration range is still checked, as in the audit.
- **Record from the wrong head or run.** The record carries the head and is verified against the log line's digest from the run that posted `build`; mismatch means unknown.
- **Large PRs.** Bounds produce `complete: false`, which means unknown, never a partial map that could hide a name.
- **Planning pass time.** Set intersections over a few thousand names per unit are milliseconds; the deadline fallback covers the unexpected.
- **Artifact API unavailable at admission.** Unknown map; admission proceeds. The record is not fetched again on a later pass, so one outage leaves one head unchecked, not blocked.
- **Deleted or renamed modules.** A PR deleting a module records nothing for it; a collision between a renamed module and its old name on `main` is a collision with `main`, caught by the PR build's audit.

## Rollout

1. Producer: the Lean script, the workflow step and the artifact, in a TauCeti PR (human-owned paths). Verify on a few PR builds that the artifact exists, the digest matches and the module list equals the lint scope.
2. Consumer, switch off: bors fetches and stores maps at admission; the journal report shows the fraction of admissions with known maps.
3. Switch on. The first collision deferral and the next lineage's audit outcome are the verification. Each step is a normal merge and automatic deployment; admissions and staging stay enabled.

## Out of scope

Collisions with `main` (covered by each PR build's audit), collisions in dependency modules, and the AI merge queue's own insertion path, which will reuse the same recorded maps when it exists.

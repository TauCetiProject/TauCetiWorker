# Declaration collisions before assembly

Detect duplicate declarations between batch-mates when a batch is assembled, from the constant names each PR adds, recorded from compiled module headers at PR build time, so that a collision costs seconds at construction instead of a full staging build per collision. The post-build audit in `scripts/Axioms.lean` stays the authority; this check only keeps known batch-mate collisions out of a candidate. It is an optimisation: when it has no record or no time, nothing changes.

## Problem

Two PRs in one batch can each declare a constant with the same fully qualified name in different modules. Each compiles alone and both compile together, so `lake build` passes; only the audit step, which loads the whole built environment and requires every TauCeti-owned name to be defined in exactly one module, fails the candidate. On 2026-11-04 the 2598 lineage spent three consecutive rounds on this (candidates 1949, 1950 and 1951: `Orthogonal/Commutant.lean` versus `Orthogonal/TwoStrands.lean`, `Identity.lean` versus `Curvature.lean`, `Morse/Cell/Band.lean` versus `Morse/Deformation/Homology.lean`), about 40 minutes of the serial staging slot each, after a clean compile every time. Removing `--fail-fast` cannot collapse these rounds because the compile never sees them.

Reading declaration names from Lean source text is not reliable (namespaces, sections, `variable`, attributes, macros). The compiled module header is: `env.header.moduleData[i].constNames` is exactly what the audit compares, it is available without elaboration, and reading it takes seconds.

## What is compared

For each PR, the set of **names it adds**: the constant names present in the PR's modules at the tested integration commit that are not present in the same modules at the merge base. A whole-module set is wrong for this purpose: a PR that edits an unrelated declaration in a module still lists every name in that module, so a batch-mate that moves one of those names to another module would be deferred although the merged tree has exactly one copy. Comparing added names against added names cannot produce that deferral: both PRs of a real batch-mate collision add the same name, in different modules, and a move adds the name in the new module only.

Excluded from the set, deliberately and without an attempt to reproduce the audit's exemption: every reserved or auxiliary name (`isReservedName`), every private name (its internal name is module-qualified and cannot collide across modules), and every name whose base header is unavailable. The audit's exemption depends on the environment the audit runs in and on declaration ranges from every module in the batch; reproducing it from one PR's build is not reliable, so the check simply does not enforce those names and the audit still does. The honest coverage statement is: user-declared, non-private names added by two batch-mates in different modules. All three collisions above are in that class.

## Design

### Record at PR build time

Every PR already receives a trusted `pr-build` run that compiles its modules against `main`. After the build, a workflow-pinned Lean script, `scripts/DeclarationNames.lean` (human-owned, same shape as `DuplicateDeclarations.check` in `scripts/Axioms.lean`), reads the headers of the modules the PR adds or modifies (the changed-module list the lint scope already computes) at the tested commit, and the headers of the same modules at the merge base from the restored `main` cache archives (`bulk-unpack-cache` keeps them; a modified module's base header is therefore available without a second build). It prints one bounded log record, the way `LINT-ENV-LOCATIONS` is printed today:

```text
DECLARATION-NAMES {"version":1,"head":"<PR head>","base":"<merge base>","tested":"<integration commit>","complete":true,"added":{"TauCeti.Geometry.Manifold.Morse.Cell.Band":["TauCeti.isZero_singularHomology_sublevelPair_of_mfderiv_ne_zero"]}}
```

Bounds: 4,096 modules, 16,384 added names per PR, 1 MiB for the record; over any bound, or with any module's base header unavailable, the record is written with `"complete": false` and no names. The step is `continue-on-error: true` and runs after the status-bearing work; it reads `.olean` headers only, never executes candidate code, and cannot change the build's verdict or fail the workflow. A PR adding only names in the excluded classes records an empty `added` map, which is a known, complete record.

### Bors stores the record after admission

Admission is unchanged. After a head is admitted, an ordinary outbox job (the existing durable background class, with its backoff) fetches the job log of the run that posted that head's `build` status, through the same bounded log collector the failure diagnostics use, and stores the parsed record with the admission snapshot. The record is accepted only if it parses completely, its `head` equals the admitted head, and its `version` is 1; anything else, including a missing line, a truncated log, a second record in the same log, or a fetch failure after the job's normal retries, stores `names: unknown`. Admission, dispatch and assembly never wait for this job. A changed head is a new admission with a new record; no record is carried between heads.

### Check at assembly

The planning pass that already tests each unit for merge conflicts against the base plus the already accepted units (`merge_selection`) gains one test per unit, before the Git replay: if any name the unit adds is also added by an already accepted unit, in a different module, the unit collides and is deferred exactly as a merge-conflicting unit is deferred today, with a request that names the constant, both modules and the other PR neutrally ("declares `x` in `M`; #N, queued earlier, declares it in `N`"). The batch is assembled without it. Every constituent head is rechecked against the live PR before the deferral is applied, as for conflicts.

Not checked: units whose record is unknown, bundles (always treated as unknown), same-module matches (two PRs adding the same name in the same module are a merge conflict or a compile error, both caught elsewhere), and anything on `main`. The check is a pure set intersection over the recorded names and runs under its own budget of two seconds per planning pass, separate from the pass's existing sixty-second deadline; when the budget is exhausted the remaining units are treated as unknown and the pass continues unchanged, logging that the check was cut short.

### Settings and observability

One runtime switch, `BORS_DECLARATION_COLLISIONS` (`off` by default; `on` once the producer has run for a day), read at each planning pass. The candidate journal records a `declaration_collision` event per deferral with the name, modules and both PRs; the admission snapshot records whether the record was present, unknown, incomplete or empty. The daily journal report counts deferrals by collision, audit collisions that still reached a candidate (expected to be the unknown-record and excluded-name cases), and the fraction of admissions with a known record.

## Failure modes considered

- **Correct PR deferred.** Only an exact user-declared name added by both PRs in different modules counts; by construction the merged tree would contain the name twice, which is what the audit rejects. Moves, edits near an existing name, and names already on `main` cannot match because they are not added by both PRs.
- **Real collision wrongly cleared.** Impossible: the check only defers; a miss (unknown record, excluded name class, a batch-mate whose record came from an older base) leaves the candidate to the audit, exactly as today.
- **Collisions with `main`.** Not covered. A PR whose build predates a landing that added the same name is caught by the audit; the earlier draft's claim that the PR build's audit covers `main` was unsound (the PR's tree does not contain the later landing) and is withdrawn.
- **Producer failure.** Non-gating step; the worst case is no record.
- **Fetch failure or delay.** Background job after admission; the worst case is an unknown record for that head. Nothing in admission or assembly waits for it.
- **Planning time.** Separate two-second budget; exhaustion means unchecked units, never a delayed or failed pass.
- **Auxiliary, generated and private names.** Excluded from enforcement rather than approximated.
- **Bundles.** Not checked; a union of constituent records could resurrect names a sibling removed.
- **Attribution.** The deferred unit is the one being tested; the request names both PRs and leaves the choice of which declaration moves to the authors, as the audit's message does.
- **Stale record after a new head.** A new head is a new admission and a new record; an old record is never reused.

## Rollout

1. Producer: the Lean script and the workflow step, in a TauCeti PR (human-owned paths). Verify on a few PR builds that the record appears, parses, carries the right head, and lists the names the diff adds.
2. Consumer, switch off: bors fetches and stores records after admission; the journal report shows the fraction of admissions with a known record.
3. Switch on. The first `declaration_collision` deferral and the next large lineage's audit outcome are the verification. Each step is a normal merge and automatic deployment; admissions and staging stay enabled.

## Out of scope

Collisions with `main`, collisions in dependency modules, the audit's exemption for explicitly declared auxiliaries, and the AI merge queue's own insertion path, which will reuse the same records when it exists.

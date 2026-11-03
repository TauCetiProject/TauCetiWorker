# Staging failure triage

Immediate changes to the existing Bors staging path, to be made before the [AI merge queue](ai-merge-queue.md). They remove the two ways the current queue wastes its single staging slot: isolating one culprit per CI round, and giving isolated PRs their own CI runs. Every change here is independently deployable, live, with admissions enabled.

## Measured problem

Observations are from the Bors candidate journal (`/repositories/1/candidates?base=main`) and the staging workflow logs for candidates 1833 through 1932, covering 2026-11-02 23:42 to 2026-11-03 22:17 UTC. At 22:50 UTC the queue held 1,154 approved PRs in 19 active batches, one running.

Over that window the staging slot was occupied for 1,379 minutes:

| Category | Runs | Slot minutes | Share |
| --- | ---: | ---: | ---: |
| Large batch, failed | 23 | 529 | 38% |
| Large batch, promoted | 7 | 262 | 19% |
| Singleton, promoted | 15 | 257 | 19% |
| Singleton, failed or invalidated | 4 | 121 | 9% |
| Small batch (at most 14), any outcome | 11 | 210 | 15% |

Seven large promotions required 23 large failures: about 3.3 failed rounds per landed batch. CI itself is short, median 17 minutes for a green run and 26 for a red one, so the cost is in the number of rounds, not their length.

Three mechanisms produce the rounds.

**One culprit per round.** `sandbox-build.sh` runs `lake build --iofail --fail-fast` for staging candidates (`IS_BATCH=true`). Every one of the 17 compiler-error failures in the window reports exactly one `✖ Building` module; the other failing lines are `bad import` fallout from jobs Lake cancelled. Compiler diagnostics attribute that module to its PR, the PR is removed, the remainder is rebuilt, and the next culprit appears. Tonight's lineage 117 → 116 → 110 → 109 removed one PR per ~30-minute round. In 8 of the 17 runs the first error arrived within 2.5 minutes of the build starting, so stopping early does save runner time on those runs; it does not save rounds, and rounds are the serial resource.

**Unattributed failures bisect.** Candidate 1930 (116 PRs) built all 62,648 jobs successfully in 22 minutes, then the audit driver reported `duplicate-declarations: SimpleGraph.Walk.IsPath.start_notMem_tail_support is declared in both TauCeti.Combinatorics.SimpleGraph.Connectivity.Menger and TauCeti.Combinatorics.SimpleGraph.Paths`. The parser recorded no observation and the batch fell to a 58/58 deterministic bisection, although both module names and their owning PRs were available. Candidates 1863 → 1864 → 1914 (55 → 27 → 14 PRs) each spent a full build and lint on the same single `unusedArguments` finding in `_private.TauCeti.LowDimTopology.DehnSurgery.Slope.0.TauCeti.slopeSetoid`, reported with `unknown: unsupported_origin`.

**Isolated PRs run alone.** When local assembly hits a merge conflict, `failure_isolation.ex` places the conflicting PR in its own sealed retry batch, and that batch receives a full staging run. Eleven such singletons ran in the window (24564, 25991, 26014, 25863, 26029, 25514, 26488 and others) and all eleven passed, at 14 to 24 minutes each. They were never broken; they conflicted textually with a batch-mate. Diagnostics-isolated PRs can also run alone: 26442 consumed 41 and then 68 minutes of the slot across two solo runs to confirm a failure the diagnostics had already located.

Located suspects themselves are handled well. A `provisional_repair` suspect receives `needs-rebase` and a head-bound repair request, leaves staging without a confirmation run, and re-enters through ordinary re-approval. Today's suspects 25774 and 26269 had repaired heads 39 and 19 minutes after being flagged.

## Changes

### 1. No solo staging runs for isolated PRs

A PR isolated from a batch by a merge conflict, by compiler or audit attribution, or by any other diagnostic takes the existing located-suspect path: `needs-rebase`, the head-bound repair request with the conflict or failure evidence, and withdrawal from the queue until re-approval. It never receives a staging build of its own. The remainder is dispatched immediately, as now.

The only batches of one are PRs approved when nothing else is waiting. The explicit `singleton` setting keeps its current meaning.

Expected effect: the singleton rows above, 28% of slot time, disappear, and the remainder batches they delayed start sooner.

### 2. Staging builds run to completion

Remove `--fail-fast` from the staging invocation in `sandbox-build.sh`. `--iofail` stays. A failed build then reports every independently broken module. Runs whose first error arrives early cost up to 20 extra runner minutes; with about three culprits per batch, finding them in one round instead of three saves one to two rounds of the serial slot per landed batch.

If the build fails, the audit and lint phases still do not run; they need a complete build. Running the duplicate-declaration audit over the modules that did compile is a possible later addition, not part of this change.

### 3. Strip every attributed culprit in one round

With complete diagnostics, the partition already supports several suspects (`failure_isolation.ex`, `[suspects, ids -- suspects]`). Each suspect takes the change 1 path individually, with its own evidence; suspects are not grouped into a shared retry batch. The remainder, with all suspects removed, is dispatched immediately.

A `bad import` of a module that no batch member and no dependency provides is attributed to the importing PR, not treated as fallout.

### 4. Attribute audit and lint failures to modules

- The parser recognises the audit driver's `duplicate-declarations: <decl> is declared in both <module> and <module>` line and maps both modules to owners through `changed_files`. An unchanged base module has no owner; every changed side is a suspect, as the existing `duplicate_declaration` handling already specifies.
- The environment lint driver (`scripts/LintEnvDriver.lean`) emits the defining module path for every finding in `LINT-ENV-LOCATIONS`. Bors uses that path directly. The private-origin name decoding added on 2026-11-03 remains as a fallback for older logs.
- Any failure the parser cannot attribute is logged with the raw failing lines and counted. Deterministic bisection remains the last resort, and the count is the measure of parser gaps still to close.

## Expected effect

Taking the window above as the baseline: singleton runs go to zero, and a landed large batch needs about two rounds (one failure that strips all culprits, one green remainder) instead of about four. The slot time per landed large batch falls from roughly 110 minutes to roughly 60, before any change to batch size, slot count or the queue model. These are projections from the measured round counts, not promised rates; the measurements below decide whether they held.

## Measurement

From the candidate journal, before and after each change:

- rounds per landed large batch (failed large candidates divided by promoted large candidates);
- slot minutes by category, as in the table above;
- singleton candidates dispatched per day, by isolation reason;
- deterministic bisections per day, with the unattributed raw lines they were caused by;
- suspects per failure partition;
- time from `provisional_repair` to the suspect's next approved head.

The journal has every field needed; no new instrumentation is required beyond the unattributed-failure counter in change 4.

## Where the changes live

| Change | Location |
| --- | --- |
| 1 | `TauCetiProject/bors-ng` `lib/worker/failure_isolation.ex`: conflict and diagnostic units route to the located-suspect path instead of a retry batch |
| 2 | `TauCetiProject/TauCetiTools` `tauceti_tools/assets/scripts/sandbox-build.sh`, pinned in `TauCeti/scripts/sandbox-build.sh` and `tauceti.toml`; remove the `IS_BATCH` `--fail-fast` branch, then bump the pin |
| 3 | `lib/worker/failure_isolation.ex` partition and the retry/withdrawal path |
| 4 | `lib/worker/failure_diagnostics.ex` parser; `TauCeti/scripts/LintEnvDriver.lean` output; the audit driver's duplicate-declaration line |

## Rollout

Deploy live, in the order 1, 2, 3, 4; each is useful alone and none depends on a later one. Keep admissions and running staging builds enabled. After each deployment, confirm the running revision, watch the next natural failure partition in the journal, and record the measurements above. No drain, no synthetic staging CI.

Rollback for each change is the ordinary code revert. Change 2 is a pin bump and reverts by restoring the pin.

## Out of scope

The queue model, batch sizing, additional staging slots and agent repair of batches in place are the subject of the [AI merge queue](ai-merge-queue.md). This plan makes the existing queue stop wasting rounds; it does not change what a round is.

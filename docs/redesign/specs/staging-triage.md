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

A PR isolated from a batch by a merge conflict, by compiler or audit attribution, or by any other diagnostic takes the existing located-suspect path: `needs-rebase`, the head-bound repair request with the conflict or failure evidence, and withdrawal from the queue until re-approval. It never receives a staging build of its own, neither as a confirmation run nor as a conflict singleton. The remainder is dispatched immediately, as now.

Two existing paths put isolated PRs back into solo CI and are removed: the conflict partition in `failure_isolation.ex` that creates a one-member sealed retry batch, and `SourceCompatibility.provisional_fallback`, which after 95 minutes without a new head creates a sealed singleton for an unchanged suspect (its repair request text promises this and is updated). The repair request asks for a rebase onto current `main`; `main` has advanced by then, so the result is a new head and ordinary readmission follows, including for a conflict-peeled PR that was never broken.

Batches of one that arise otherwise (a PR approved when nothing else is waiting, a two-member batch minus one suspect, an explicit `singleton` setting) keep running as they do today; no scheduling is added to prevent them. Retry batches that already exist at deployment drain under the old rules.

Expected effect: the confirmation and conflict singletons, most of the 28% of slot time in the singleton rows, stop being dispatched, and the remainder batches they delayed start sooner.

### 2. Staging builds run to completion

Remove `--fail-fast` from the staging invocation in `sandbox-build.sh`. `--iofail` stays. A failed build then reports every independently broken module. Runs whose first error arrives early cost up to 20 extra runner minutes; with about three culprits per batch, finding them in one round instead of three saves one to two rounds of the serial slot per landed batch.

If the build fails, the audit and lint phases still do not run; they need a complete build, so an audit or lint culprit is found only after the compiler culprits are gone. A module that fails because its import failed is still hidden behind that import; only independently broken modules become visible. Running the duplicate-declaration audit over the modules that did compile is a possible later addition, not part of this change.

### 3. Strip every attributed culprit in one round

With complete diagnostics, the partition already supports several suspects (`failure_isolation.ex`, `[suspects, ids -- suspects]`). Only directly attributed owners are suspects: a PR whose changed file is named in a compiler error or an audit finding. The partition's expansion of suspects through shared paths and import components stays a retry-grouping hint and does not withdraw anyone. Each suspect takes the change 1 path with its own evidence; suspects are not grouped into a shared retry batch.

The handoff withdraws all suspects, creates the remainder once and retires the parent in one transaction, as the existing single-suspect `provisional_failure` does. It cannot be done by calling that helper once per suspect: `split` treats an existing `provisional_repair` event on the candidate as completion and returns `:retrying`. An empty remainder (every member attributed) completes the parent without a new batch. The remainder is dispatched immediately.

A speculative successor that fails while its predecessor has not landed keeps today's behaviour (`independent_provisional_base?` declines the handoff) because its failure cannot be assigned to either batch; it is re-evaluated once the predecessor's outcome is known.

### 4. Attribute audit and lint failures to modules

- The log parser is `priv/failure_diagnostics.py` in bors-ng; the Elixir side maps its observations to owners. Its `AUDIT` pattern matches only the import-collision form (`audit-driver: import <module> failed, environment already contains '<decl>' from <module>`). Add the duplicate-declaration form, `duplicate-declarations: <decl> is declared in both <module> and <module>`, producing the same `duplicate_declaration` observation; owner mapping through `changed_files` already treats every changed side as a suspect and an unchanged base module as ownerless.
- The environment lint driver (`scripts/LintEnvDriver.lean`) emits the defining module path for every finding in `LINT-ENV-LOCATIONS`. The parser's `environment_locations` schema currently requires `path`, `line` and `column` all present and positive; it accepts a record with a module path and no range. The private-origin name decoding added on 2026-11-03 remains as a fallback for older logs.
- `bad import` lines do not match the parser's compiler-error patterns (both require `file:line:col`), so fail-fast fallout is not attributed today and will not be after change 2. Nothing is added for them.
- Any failure the parser cannot attribute is logged with the raw failing lines and counted. Deterministic bisection remains the last resort, and the count is the measure of parser gaps still to close.

## Expected effect

Taking the window above as the baseline: confirmation and conflict singletons stop, and a landed large batch needs fewer rounds, with two (one failure that strips every independently broken module, one green remainder) as the optimistic case. Rounds remain for modules hidden behind a failed import, for audit and lint findings that only a green build exposes, and for anything still unattributed. The 3.3 failed rounds per landing above also include repeated bisections of a single unattributed finding, so it does not establish three independent culprits per batch. Roughly 60 slot minutes per landed large batch, from roughly 110 today, is the target; the measurements below decide whether it was met.

## Measurement

From the candidate journal, before and after each change:

- failed rounds per landed large batch, both as the window aggregate (failed large candidates divided by promoted large candidates, 3.3 in the window above) and per completed lineage through the journal's recorded parent candidate IDs, counting the smaller descendants a split produces;
- slot minutes by category, as in the table above, with singletons split into confirmation or conflict runs (the ones change 1 removes) and remainders of one;
- deterministic bisections per day, with the unattributed raw lines that caused them;
- suspects per failure partition;
- time from `provisional_repair` to the suspect's next admission, and the number of suspects still withdrawn, so a PR that never comes back is visible rather than absent.

The journal has every field needed; no new instrumentation is required beyond the unattributed-failure counter in change 4.

## Where the changes live

| Change | Location |
| --- | --- |
| 1 | `TauCetiProject/bors-ng` `lib/worker/failure_isolation.ex`: conflict and diagnostic units route to the located-suspect path instead of a retry batch; `lib/worker/source_compatibility.ex`: remove `provisional_fallback` and its 95-minute grace |
| 2 | `TauCetiProject/TauCetiTools` `tauceti_tools/assets/scripts/sandbox-build.sh`, pinned in `TauCeti/scripts/sandbox-build.sh` and `tauceti.toml`; remove the `IS_BATCH` `--fail-fast` branch, then bump the pin |
| 3 | `lib/worker/failure_isolation.ex` partition and `lib/worker/source_compatibility.ex` `provisional_failure`, generalised to a set of suspects in one transaction |
| 4 | `priv/failure_diagnostics.py` patterns and location schema, `lib/worker/failure_diagnostics.ex` owner mapping; `TauCeti/scripts/LintEnvDriver.lean` output |

## Rollout

Deploy live, in the order 1, 2, 3, 4; each is useful alone and none depends on a later one. Keep admissions and running staging builds enabled. After each deployment, confirm the running revision, watch the next natural failure partition in the journal, and record the measurements above. No drain, no synthetic staging CI.

Rollback for each change is the ordinary code revert. Change 2 is a pin bump and reverts by restoring the pin.

## Out of scope

The queue model, batch sizing, additional staging slots and agent repair of batches in place are the subject of the [AI merge queue](ai-merge-queue.md). This plan makes the existing queue stop wasting rounds; it does not change what a round is.

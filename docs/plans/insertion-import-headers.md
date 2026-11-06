# Refuse an insertion that strands an importer

Implements acceptance scenario 25 of [the merge queue plan](../redesign/specs/ai-merge-queue.md): a PR that removes or renames a module still imported by an unchanged module on `main` or by a batch-mate is refused at insertion from the import headers, with the ordinary missing-source request, and never reaches head CI.

## Problem

The insertion probe (`Controller._probe` in `tauceti_queue/controller.py`) replays the approved squash onto the open batch's tip with `chain.build` and inserts it when the replay is clean. A deletion is always textually clean, so a PR that deletes `TauCeti/Algebra/Lie/Subalgebra/Fixed.lean` inserts even when `A3.lean`, added to `main` after the PR's head was built, still imports it. Both heads are individually green; only the head's CI, in its import-header preflight a few minutes in, fails the whole batch. The queue then handles it as a source failure: a repair task, an agent session, a second CI run. With one- to four-PR heads that is one lost cycle; with hundred-PR heads it is the batch's cycle. On 2026-11-04 the same case (#26731 against #26762) cost Bors a 123-PR head build and a bisection.

`admission_planning.Planner` already parses import headers (`imports()`, a conservative scan that stops at the first non-header line) for repair navigation, cached by blob. Nothing reads them to decide an insertion.

## Change

### 1. A whole-tree gap scan, cached by tree

Add `admission_planning.import_gaps(planner, tree) -> Gaps | None`:

- `git ls-tree -r -z <tree>` restricted to `TauCeti.lean` and `TauCeti/**/*.lean`, regular blobs only; a symlink or a non-blob entry under `TauCeti/` makes the scan unavailable (`None`), the same rule the CI preflight applies.
- Headers through `Planner.headers`, keyed by blob, so the second and later scans of a tree that shares almost every blob with the previous tip read only the changed files. Reuse the existing byte budgets (`MAX_FILE_BYTES`, `MAX_BLOB_BYTES`); over budget is unavailable, not partial.
- A gap is `(importer_path, module)` where `module` starts with `TauCeti.` and `module.replace('.', '/') + '.lean'` is not in the listing. Imports outside the project are not inspected.
- Memoise the result per tree id in the planner (`self.gaps: dict[str, Gaps | None]`), bounded to the last 64 trees. The open batch's current tip is scanned once per insertion pass, not once per probe.

### 2. The probe refuses a new gap

In `_probe`, after `chain.build` returns `CLEAN` and before the membership transaction:

```
before = import_gaps(planner, tree_of(batch["tip"]))
after = import_gaps(planner, tree_of(result.tip))
new = sorted(after - before) if before is not None and after is not None else []
```

If `new` is non-empty, the PR is refused the way a conflict with `main` is refused today (`against_main` branch of `_probe`): the input goes to `INPUT_CANCELLED` with `reason="missing-source"`, `last_probe` holds the gaps, the claim is released with `"missing-source"`, a `probe-missing-source` journal entry records `number`, `batch_id`, `tip`, and the gaps, and `_missing_source_request` enqueues one comment under `missing-source:{number}:{head_sha}`:

> This PR removes `TauCeti/Algebra/Lie/Subalgebra/Fixed.lean`, which `TauCeti/Algebra/Lie/SpecialLinear/GraphAutomorphism/A3.lean` still imports (`TauCeti.Algebra.Lie.Subalgebra.Fixed`). Keep the module or update every importer, then approve it again.

or, when the importer is among the PR's own changed files:

> This PR imports `TauCeti.X` from `TauCeti/Y.lean`, but no source provides it. Fix the import, then approve it again.

The two wordings come from one rule: a gap whose importer the PR did not touch is a removal; a gap whose importer the PR changed is a bad import. The PR's changed paths are already in `Planner.changes` for its squash commit (`result.commits[0].sha`). At most eight gaps are listed.

Comparing the prospective tip with the current tip, rather than with `main`, attributes every new gap to the PR being inserted and nothing else: a batch-mate that removed a module earlier was itself refused at its own insertion, and a gap already present on the batch's base is `main`'s own and appears in both scans. No ownership mapping is needed.

Unavailable scans (`None` on either side) insert the PR exactly as today and journal `probe-import-scan-unavailable` with the reason; the CI preflight remains the authority. The scan never changes a verdict, only refuses an insertion that CI would refuse anyway.

### 3. Re-entry

The refused input is cancelled, so it re-enters through re-approval after the author (or the review pipeline) pushes a fix, exactly as `conflicts-with-main` does. A PR whose gap was caused by `main` moving (the importer landed after its head) needs a new head in any case; a PR whose gap is a bad import of its own needs a new head too. No `needs-rebase` label and no withdrawal row: those are for members already inserted.

### 4. Status and counters

`_count("missing_source_refusals")`; the journal report (`docs/queue/journal-report.md`) gains a line counting `probe-missing-source` events and `probe-import-scan-unavailable` reasons, so an unavailable scan that recurs (a repository with a symlink under `TauCeti/`, a budget that is too small) is visible rather than silent.

## Failure modes considered

- **Wrong refusal.** Only a `TauCeti.*` import whose source path is absent from the prospective tree, and present or absent identically in the current tip, counts. The header scanner is the one already used for navigation and reads only the header; a misparse would have to yield a project module name that does not exist, and such an import fails the CI preflight regardless. A PR that deletes a module and updates every importer produces no gap; a rename with updated importers produces no gap.
- **Slow probe.** One `ls-tree` per scan plus the headers of blobs not yet cached; the current tip's scan is shared across the pass. A cold cache reads every header once (bounded by `MAX_BLOB_BYTES`); every later pass reads only new blobs. The probe already runs without the controller lock.
- **Scan unavailable.** The insertion proceeds as today; the journal says so.
- **Batch-mate interactions.** A PR that imports a module a batch-mate adds inserts (the module is in the tip); a PR that imports a module nobody provides is refused with the bad-import wording; a PR that removes a module a batch-mate imports is refused with the removal wording naming that batch-mate's file.
- **Empty or edited contributions.** The scan runs on the exact prospective tip, whatever produced it.

## Tests

- `tests/queue_admission_planning.py`: `import_gaps` on a temporary repository: a clean tree; a removed provider with an unchanged importer; a bad import; a gap already on the base; a symlink making the scan unavailable; the per-tree memo and the shared blob cache (count `cat-file` calls).
- `tests/queue_fixture.py`, `test_s25_removal_that_strands_an_importer_is_refused_at_insertion`: `main` has `Provider.lean` and `Importer.lean` importing it; PR 101 deletes `Provider.lean` and is refused with the removal comment, its input cancelled, no member added, the batch tip unchanged; PR 102 deletes `Provider.lean` and updates `Importer.lean` and inserts; PR 103 adds `Consumer.lean` importing `TauCeti.Gone` and is refused with the bad-import wording; a PR inserted after a batch-mate added `New.lean`, importing it, inserts. Add the scenario to the mapping in `docs/queue/controller.md`.
- The existing s1 and s9 scenarios keep passing unchanged: a clean insertion runs two scans whose result is empty.

## Rollout

One PR to `TauCetiWorker` `main`, deployed to the queue host through the queue's existing release path (the service reports its running revision in `/status`). Verification: the next PR of this class is refused with a `probe-missing-source` journal entry and its comment, and no head fails in the "Preflight exact-candidate import headers" step for a reason this scan covers. Nothing changes for PRs without a gap.

# Mathlib overlap maintenance

`overlap-scan` investigates Mathlib changes that may supersede Tau Ceti code.
`overlap-fix` adopts that API through upstream refactor PRs, preserving additional
Tau Ceti mathematics. PR authors remain responsible for checking existing Mathlib.

## Setup and operation

Initialize once with an account that can push to `TauCetiProject/tauceti-claims`
and has write access to Tau Ceti to create and apply the `mathlib-overlap` label:

```sh
tauceti overlap init --mathlib-commit 66748b489336a59ed4b4a4a612615c38de823e9a
tauceti work --only overlap-scan
tauceti work --only overlap-fix
tauceti work --loop --only overlap-scan,overlap-fix
```

The initial commit is Tau Ceti's first Mathlib pin, from June 2, 2026. Initialization
is create-only; it never moves an existing cursor. `--github-account LOGIN`
selects credentials without changing gh's active login.

The calibrated default is 160 first-parent commits. `--overlap-batch-commits N`
overrides `TAUCETI_OVERLAP_BATCH_COMMITS`; loop children inherit the setting.
Persistent workers can set that variable in their existing `env` table. During
normal rounds, repair then scan run after existing PR maintenance and review,
before new roadmap authoring. Busy maintenance queues can defer them; use a
focused worker for steady catch-up. Dashboard keys `9` and `0` select
scan and repair; previous keys remain unchanged. `--pr` excludes both phases.

Scanning runs on the host even with `--bubble`: its inputs are local source
snapshots and the host publishes issues. Repair supports ordinary host and
Bubble authoring, provider selection, and pacing.

## Coordination and recovery

The authoritative cursor is a tiny checkpoint commit behind
`refs/tauceti-state/mathlib-overlap` in the shared claim repository. Its payload
contains the Mathlib SHA; Mathlib's objects are not transferred. Lease garbage
collection cannot delete this state namespace.

Scan rounds acquire `mathlib-overlap-scan`; repairs acquire `overlap-fix/<issue>`.
Owners include account, worker ID, and a unique run identifier. Leases are
heartbeated. Shared write access and successful coordination are mandatory:
neither phase proceeds unclaimed or falls back to an account-specific fork.
Scanning additionally requires Tau Ceti write access so GitHub accepts issue
labels; repair workers can still contribute through forks without that access.

A scanner rereads the cursor after acquisition and freezes Tau Ceti main and its
pin. It examines consecutive commits, including merge patches against their first
parent. The structured result must account for every supplied commit. Incomplete
investigations leave the cursor unchanged. After every finding is published, a
conditional Git push advances the checkpoint; stale pushes fail. A reverted or
off-history pin suppresses automatic scanning for that round while the cascade
can continue to other work; a focused scan reports the invalid range explicitly.
Lease cleanup failures are logged and left to expiry, preserving completed work.

Each labelled issue contains declaration mappings, commit-specific evidence, API
differences, and an explicit preservation paragraph. Stable markers identify
findings across retries, including already-closed issues. Completed local results
can be reused after interrupted publication when the cursor, pin, and commit range
still match; otherwise the range is investigated again. Rare duplicate issues
remain possible after interrupted requests. Cursor correctness does not depend
on issue uniqueness. Publication validates the returned label and finding marker
before advancing. If GitHub confirms an issue number but drops its metadata, a
local receipt blocks duplicate creation and reconciles that same issue on retry;
the error identifies the metadata that must be restored.

Repair selection checks open PRs from all accounts for the finding marker, respects
human holds and authoring backpressure, and revalidates the overlap against the
current pin. It removes superseded declarations and compatibility shims, migrates
callers, and preserves additional functionality. Existing review/fix/CI phases
maintain the resulting PR. `Fixes #issue` closes the issue on merge. False positives
can be closed with a concrete explanation.

## Calibration

Scan conversations, batch manifests, results, and metrics live under
`state/<worker>/overlap-runs/<run>/`. Repair conversations use `logs/<worker>/`.
Telemetry includes provider/model, duration, patch size, commit count, completion,
and available provider usage and compaction events. Cumulative input-token totals
are not peak context occupancy. Zero compaction events means none was reported,
not proof that a provider could not compact internally.

Commissioning includes running the phases and reading their conversations. Compare
smaller and larger batches, inspect at least five completed representative scans,
verify actual examination and focused searching, and exercise real repairs.
The [calibration report](mathlib-overlap-calibration.md) records nine inspected
live scans, repair trials, and the revised choice of 160. The managed catch-up
runs used Codex `gpt-6.1-sol` at high effort; native session usage and compaction
metadata supplemented the readable transcripts. Tune the override for other
profiles and unusually demanding batches.
Compaction is a tuning signal, not grounds to discard valid completed findings.

V1 has no Tau Ceti cursor, exhaustive semantic guarantee, negative-finding database,
or separate coverage ledger. The cursor means “examined through this Mathlib commit.”

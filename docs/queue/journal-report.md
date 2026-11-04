# Staging journal report

`scripts/staging_journal_report.py` turns the Bors candidate journal into the measurements the
[staging triage spec](https://github.com/TauCetiProject/TauCetiWorker/blob/overhaul/docs/redesign/specs/staging-triage.md)
defines: slot occupancy by category, failed rounds per landed large batch, singleton and
bisection counts per day, suspects per failure partition, and the time from a repair handoff to
the suspect's readmission. It exists so every change to staging, and later the merge queue
service, has a before and an after computed the same way. The baseline for the triage window is
committed below.

## Running it

The journal is `https://bors.taucetiproject.org/repositories/1/candidates?base=main` (schema
`tauceti-bors.candidates/v1`): the 100 most recent candidates with their events, plus the 500
most recent admissions, timestamps in unix microseconds. Save snapshots; the feed is a rolling
window and older candidates leave it.

```sh
# Live feed, Markdown to stdout
uv run python scripts/staging_journal_report.py --url 'https://bors.taucetiproject.org/repositories/1/candidates?base=main'

# Saved snapshots, merged by candidate id (the later snapshot wins for a candidate in both),
# restricted to a candidate range, with both outputs written to files
uv run python scripts/staging_journal_report.py \
    --journal candidates-2026-11-03T2251Z.json --journal candidates-2026-11-04T0416Z.json \
    --from-candidate 1833 --to-candidate 1932 \
    --markdown report.md --json report.json

# A time window on the dispatch time (UTC)
uv run python scripts/staging_journal_report.py --journal snapshot.json --since 2026-11-03T00:00Z --until 2026-11-04T00:00Z

# Regenerate the test fixture from real snapshots
uv run python scripts/staging_journal_report.py --journal a.json --journal b.json --reduce \
    --candidates 1843,1861,1862,1863,1864,1866,1894,1914,1876,1917,1918,1883,1884,1885,1920,1906,1908,1911,1929,1936,1937 \
    --output tests/fixtures/staging-journal-fixture.json
```

The script is stdlib only (Python 3.11+). The JSON document carries `schema:
tauceti.staging-journal-report/v1`, every per-run row with its timing source, every lineage,
partition and handoff, and the list of candidates the rules could not classify. The Markdown is
the same data as tables. `tests/staging_journal_report.py` checks the classifier against the
fixture with hand-verified numbers.

## Classification rules

The module docstring of the script is the normative list; this is the summary.

- **Run.** A candidate that was dispatched (`github.workflow_dispatch_response dispatched`, else
  `bors.outbox dispatch_requested`). Everything else is a non-run, counted by reason:
  construction conflict (`local_conflict`), construction import failure, construction stopped,
  or superseded before dispatch (a batch generation replaced by a later generation).
- **Slot minutes.** `runner_started_at` to `runner_finished_at` when the feed supplies them. It
  never does today (`unknown_reason: runner lifecycle is not supplied by commit statuses`), so
  the fallback is dispatch to the earliest terminal event (`terminal_observed`, `promoted`,
  `train_invalidated`, `cancellation_terminal`), or to the last trusted `failure_classified` when
  no terminal event was journaled. Each run records which source it used.
- **Outcome.** `promoted`, `failed`, `cancelled`, `invalidated`, decided by the earliest terminal
  event. A dispatched candidate with none is unclassified ("dispatched, no outcome in journal").
- **Size.** Large is 15 or more members, small is 2 to 14, singleton is 1.
- **Singleton kind.** From the member's `source` and `retry_reason`: `admitted_alone`
  (`bors.admission`), `conflict` (`structured_conflict`, `conflict_isolation`,
  `merge_conflict_deferred`; the parent's `local_conflict` is checked to name the PR),
  `confirmation` (a located suspect retried alone: `compiler_diagnostics`,
  `bounded_provider_hint`, `import_provider_hint`, `recovered_parent_diagnostics`,
  `missing_import`), `remainder` (`provisional_remainder`, `diagnostic_remainder`,
  `member_removed`), `bisection` (`bisection`, `dependency_component`,
  `balanced_dependency_components`). Neutral retries (`train_invalidated`, `stale_dispatch`,
  `cancellation`, `non_fast_forward`, `provisional_unconfirmed`) inherit the parent's kind.
- **Lineages.** Follow the `parent_candidate_id` most members carry. A lineage is complete when
  every leaf is promoted, superseded, quarantined or a handoff with no remainder. Failed rounds
  count every failed run in the lineage, whatever its size.
- **Bisections.** `failure_partition` with strategy `deterministic_bisection` and at least two
  groups. With no groups it is an indivisible failure (the PR is quarantined), reported
  separately, as are the `balanced_dependency_components` splits. Each entry lists the
  `failure_diagnostics` observations the parser recorded, or says that it recorded none.
- **Suspects per partition.** The PRs of the `provisional_repair` that follows the partition,
  else the first group of a located strategy, else zero.
- **Handoff to readmission.** From each `provisional_repair` to the suspect PR's earliest later
  appearance at a different head, as a member of a later candidate or as a `bors.admission`
  record. Suspects without one are listed as still withdrawn at the data horizon.

## Baseline: triage window, candidates 1833 to 1932

Computed from the snapshot saved at 2026-11-03 22:51 UTC (the exact window of the triage spec's
"Measured problem" table; 100 candidates, 60 runs, first dispatch 2026-11-02 23:44 UTC, last run
end 2026-11-03 22:17 UTC).

The staging slot was occupied for 1394 minutes.

| Category | Runs | Slot minutes | Share |
| --- | ---: | ---: | ---: |
| Large batch, failed | 23 | 544 | 39% |
| Large batch, promoted | 7 | 262 | 19% |
| Large batch, cancelled or invalidated | 6 | 103 | 7% |
| Singleton, promoted | 15 | 257 | 18% |
| Singleton, failed, cancelled or invalidated | 4 | 121 | 9% |
| Small batch (2 to 14), any outcome | 4 | 107 | 8% |
| Dispatched, no outcome in journal | 1 | 0 | 0% |
| Total | 60 | 1394 | 100% |

Median run: 16.8 minutes for a promoted run (24 runs), 26.3 minutes for a failed run (27 runs).

| Kind | Runs | Slot minutes | Promoted | Failed | Cancelled or invalidated | Candidates |
| --- | ---: | ---: | ---: | ---: | ---: | --- |
| conflict | 10 | 148 | 9 | 0 | 1 | 1836, 1840, 1884, 1885, 1890, 1891, 1892, 1893, 1920, 1927 |
| confirmation | 3 | 120 | 0 | 2 | 1 | 1906, 1908, 1911 |
| remainder | 0 | 0 | 0 | 0 | 0 |  |
| bisection | 0 | 0 | 0 | 0 | 0 |  |
| admitted_alone | 6 | 110 | 6 | 0 | 0 | 1844, 1845, 1848, 1865, 1899, 1929 |
| unknown | 0 | 0 | 0 | 0 | 0 |  |

Failed rounds per landed large batch: 23 failed large runs / 7 promoted large runs = 3.29. Six
lineages with a large root are complete in the window (5 failed runs / 6 promoted runs = 0.83);
the lineages that carry most of the failures (1849, 1861, 1867, 1873, 1879, 1886) are still
open at the end of the window, so the per-lineage figure undercounts until their retries land.

Per day: 2026-11-02 (23:42 onward) 1 singleton run, 1 component split; 2026-11-03 18 singleton
runs, 4 deterministic bisections (candidates 1843, 1876, 1882, 1930), 3 component splits
(1863, 1864, 1914), 8 indivisible quarantines. Three of the four bisections (1876, 1882, 1930)
have no diagnostics observation at all; 1843's recorded a `compiler_error` with a path but was
bisected regardless. All three component splits were caused by the same `unusedArguments`
finding on `_private.TauCeti.LowDimTopology.DehnSurgery.Slope.0.TauCeti.slopeSetoid` reported
with `unknown: unsupported_origin`.

Suspects per partition: 52 partitions, 35 with one suspect, 1 with two (candidate 1919,
`missing_import`), 16 with none (12 deterministic bisections, of which 8 are indivisible
singleton quarantines, 3 component splits, 1 dependency component). 11 handoffs to repair.

Handoff to readmission: 11 suspects handed off, 5 readmitted within the snapshot (median 51.1
minutes, min 29.4, max 121.9), 6 still withdrawn at 22:50 UTC: PRs 23638, 25316, 25774, 26251,
26269, 26751.

Unclassified: candidate 1932, dispatched at 22:19 UTC and still running when the snapshot was
taken. Non-runs: 22 construction conflicts, 17 generations superseded before dispatch, 1
construction import failure.

### Comparison with the triage spec's published table

The spec reports 1,379 slot minutes across 23 large failed (529), 7 large promoted (262), 15
singleton promoted (257), 4 singleton failed or invalidated (121) and 11 "small batch" (210)
runs. The report reproduces four of the five rows to the minute: large promoted 262.5, singleton
promoted 257.5, singleton failed or invalidated 121.4 (candidate 1911 has no `terminal_observed`
and is timed to its trusted `failure_classified`, 10.4 minutes, which is the only way to reach
the published 121). The remaining differences:

- **The "small batch, any outcome" row of 11 runs and 210 minutes is a mixture.** The window
  has four runs of 2 to 14 members (1843, 1866, 1913, 1914; 107 minutes). The other seven runs
  behind the published row are the three cancelled large batches (1855, 1857, 1912; 50 minutes),
  the three invalidated large batches (1896, 1910, 1915; 53 minutes), and 1932, dispatched but
  without an outcome. 107 + 50 + 53 = 210. The report gives these their own rows ("Large batch,
  cancelled or invalidated" and "Dispatched, no outcome in journal") because the triage changes
  target rounds, and a cancelled or invalidated 120-PR batch is not a small batch.
- **Large batch failed: 544 minutes here against 529.** The 23 runs agree. Twenty-two have a
  `terminal_observed` error and sum to 518 minutes; the 23rd, candidate 1862 (56 members), was
  retired by its repair handoff before a status summary was journaled and is timed to its
  trusted `failure_classified` at 26.3 minutes. How the published figure timed 1862 is not
  recorded; the 15-minute (3%) gap between the totals (1394 against 1379) sits entirely in this
  row.
- **Failed rounds per landed batch** agree: 23 / 7 = 3.29, published as 3.3.
- **Conflict singletons.** The spec says eleven ran and all passed. The report classifies ten
  singleton runs as conflict-origin: nine promoted (1836, 1840, 1885, 1890, 1891, 1892, 1893,
  1920, 1927) and 1884, which was invalidated by the speculative train after 1.2 minutes and
  rerun as 1885. Every PR the spec names (24564, 25991, 26014, 25863, 26029, 25514, 26488) is
  among them; the eleventh in the spec was most likely 1884 and 1885 counted as two passing runs,
  or one of the six singletons admitted alone. The 26442 confirmation runs match the spec's "41
  and then 68 minutes" (1906 at 41.5 minutes, invalidated; 1908 at 68.3 minutes, failed).
- **Handoff timing.** The spec's "repaired heads 39 and 19 minutes after being flagged" for
  25774 and 26269 is the time to a pushed head. The report measures the later readmission to
  the queue; in this snapshot both were still withdrawn. In the later snapshot below, 25774 was
  readmitted 230 minutes after its handoff and 26269 was still withdrawn at 04:12 UTC.

## Post-triage-deployment window, candidates 1841 to 1940

Computed from the snapshot saved at 2026-11-04 04:16 UTC. bors-ng PRs #94 and #95 (the staging
triage changes) landed on 2026-11-04 and may have been only partially deployed for part of this
window, and the window overlaps the baseline window by 92 candidates; these numbers are a record
of the feed at that time, not a measurement of the triage changes' effect.

The staging slot was occupied for 1653 minutes (61 runs, first dispatch 2026-11-03 01:18 UTC,
last run end 2026-11-04 04:05 UTC).

| Category | Runs | Slot minutes | Share |
| --- | ---: | ---: | ---: |
| Large batch, failed | 20 | 539 | 33% |
| Large batch, promoted | 8 | 432 | 26% |
| Large batch, cancelled or invalidated | 6 | 103 | 6% |
| Singleton, promoted | 16 | 308 | 19% |
| Singleton, failed, cancelled or invalidated | 5 | 141 | 8% |
| Small batch (2 to 14), any outcome | 5 | 130 | 8% |
| Dispatched, no outcome in journal | 1 | 0 | 0% |
| Total | 61 | 1653 | 100% |

Median run: 23.1 minutes for a promoted run (26 runs), 26.5 minutes for a failed run (24 runs).

| Kind | Runs | Slot minutes | Promoted | Failed | Cancelled or invalidated | Candidates |
| --- | ---: | ---: | ---: | ---: | ---: | --- |
| conflict | 8 | 119 | 7 | 0 | 1 | 1884, 1885, 1890, 1891, 1892, 1893, 1920, 1927 |
| confirmation | 3 | 120 | 0 | 2 | 1 | 1906, 1908, 1911 |
| remainder | 1 | 19 | 0 | 0 | 1 | 1937 |
| bisection | 0 | 0 | 0 | 0 | 0 |  |
| admitted_alone | 9 | 190 | 9 | 0 | 0 | 1844, 1845, 1848, 1865, 1899, 1929, 1933, 1935, 1939 |
| unknown | 0 | 0 | 0 | 0 | 0 |  |

Failed rounds per landed large batch: 20 / 8 = 2.5 (lineage 1867 completed with a 108-PR
promotion, candidate 1934, after the baseline snapshot). Seven complete lineages with a large
root: 8 failed runs / 9 promoted runs = 0.89. Per day: 2026-11-03 18 singleton runs, 4
bisections, 3 component splits, 6 indivisible quarantines; 2026-11-04 (to 04:12) 3 singleton
runs, 1 indivisible quarantine (candidate 1938, a construction conflict). 48 partitions, 11
handoffs; 8 suspects readmitted (median 167.2 minutes, min 29.4, max 1294.2), 3 still withdrawn
at 04:12 UTC: PRs 23638, 25202, 26269. Unclassified: candidate 1940, dispatched at 04:07 UTC
and still running. Non-runs: 23 construction conflicts, 15 superseded generations, 1
construction import failure.

Merging the two snapshots (`--journal` twice, window 1833 to 1932) resolves the baseline's one
unclassified candidate: 1932 failed after 50.4 minutes with a handoff of PR 25202, which moves
the large-failed row to 24 runs and 595 minutes and the total to 1445 minutes.

## What the journal does not say

- The runner lifecycle fields are always null, so slot minutes are dispatch-to-terminal and
  include the few seconds between the dispatch response and the GitHub run's creation.
- `member_removed` retries carry no `parent_candidate_id` (the retry is cloned from a batch
  that had no candidate yet), so a lineage breaks at a member removal: 1837, 1839, 1896 and
  1910 appear as roots.
- The admissions list holds the 500 most recent admissions only. Readmissions older than that
  are found through later candidate membership, which lags the admission by the time the PR
  waits in a batch; the report takes the member's `original_admitted_at` when it is after the
  handoff.
- `provisional_repair` events in these snapshots carry a single `pr` and `head_sha`. The
  current bors-ng writes `prs` and `suspects` (a set of suspects per handoff) beside them; the
  report reads both forms.
- The suspects of a located partition without a handoff are taken as the first group of
  `failure_partition.groups`; `failure_isolation.ex` writes the suspects first, and the
  `provisional_repair` that follows a handoff confirms it in every case in these snapshots.

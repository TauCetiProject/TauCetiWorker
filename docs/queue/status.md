# AI merge queue: implementation status

Running record for the work plan in
[`docs/redesign/specs/ai-merge-queue.md`](../redesign/specs/ai-merge-queue.md): what is merged,
what is blocked on which operator item, and the latest experiment numbers. Updated by the
orchestrating session as streams land.

## Increment 0

| Stream | Branch | State |
| --- | --- | --- |
| A. Attributed chain tool (`tauceti_queue/chain.py`) | `queue/chain-tool` | merged, https://github.com/TauCetiProject/TauCetiWorker/pull/474 |
| B. Historical repair experiments (`tauceti_queue/repair_experiment.py`, `docs/queue/repair-experiments.md`) | `queue/repair-experiments` | running on chungus2; harness PR https://github.com/TauCetiProject/TauCetiWorker/pull/477 open, results pushed to it as candidates complete |
| C. Journal report (`scripts/staging_journal_report.py`, `docs/queue/journal-report.md`) | `queue/journal-report` | merged, https://github.com/TauCetiProject/TauCetiWorker/pull/475; baseline for candidates 1833 to 1932 in [journal-report.md](journal-report.md) |
| D. Readmission age in Bors (`queue_age.ex`, handoff record) | bors-ng | merged: https://github.com/TauCetiProject/bors-ng/pull/99 (inherit the age once per handoff), https://github.com/TauCetiProject/bors-ng/pull/100 (same head after a lifted conflict request), https://github.com/TauCetiProject/bors-ng/pull/102 (a retired handoff parent no longer blocks the automatic re-offer); deployed through the automatic Cloudflare build, verification waits for the next `provisional_repair` whose `suspects[]` carry `original_admitted_at` (none since 04:42 UTC) |
| GitHub App | operator item 1 | created: `tauceti-queue`, App ID 5607007, credentials in `~/.config/tauceti-queue/` on chungus2; installed on `TauCetiProject/TauCeti` (installation 178451644), verified with the stored key |
| Dedicated staging runners | operator item 3 | investigation in [staging-runners.md](staging-runners.md); workflow change https://github.com/TauCetiProject/TauCeti/pull/27526 open; Namespace profile and `STAGING_RUNS_ON` waiting on the operator |

## Operator items outstanding

1. Done: the `tauceti-queue` App is created and installed on `TauCetiProject/TauCeti`.
2. Create `TauCetiProject/TauCetiQueueScratch` (needed by stream I, not by increment 0).
3. Reserve dedicated staging capacity: a Namespace runner profile `tauceti-staging` (ubuntu-24.04, 16x32, amd64,
   concurrent runners per profile 3) in the workspace behind the `namespace-managed-runners` App, confirm the
   workspace concurrency limit exceeds `NAMESPACE_BUILD_CAP` (20) by 3 or lower the cap, prove the label is served,
   then set the TauCeti repository variable `STAGING_RUNS_ON` to
   `["namespace-profile-tauceti-staging;container.privileged=true;job.priority=0"]` (exact label from the profile page)
   once https://github.com/TauCetiProject/TauCeti/pull/27526 is merged. Details and the fleet-host alternative in [staging-runners.md](staging-runners.md).
4. Provide the service host (increment 1).

## Latest numbers

Journal baseline (candidates 1833 to 1932, 2026-11-03): 1394 slot minutes, 23 failed large rounds per
7 landed large batches (3.3), 10 conflict and 3 confirmation singletons, 11 handoffs of which 5 were
readmitted (median 51 minutes). Full tables in [journal-report.md](journal-report.md).

Repair experiments (interim, 2026-11-04 05:45 UTC, lane running): 4 of 4 completed candidates green
(1931, 1932, 1930, 1843), agent time 57 s to 175 s, estimated inference cost $0.40 to $0.54 each; the
candidate whose modules were already in the warm pool prepared in 16 s. The sample continues toward
twenty candidates; the table and the warm-artifact decision land in `repair-experiments.md` with
stream B.

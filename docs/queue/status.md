# AI merge queue: implementation status

Running record for the work plan in
[`docs/redesign/specs/ai-merge-queue.md`](../redesign/specs/ai-merge-queue.md): what is merged,
what is blocked on which operator item, and the latest experiment numbers. Updated by the
orchestrating session as streams land.

## Increment 0

| Stream | Branch | State |
| --- | --- | --- |
| A. Attributed chain tool (`tauceti_queue/chain.py`) | `queue/chain-tool` | in progress |
| B. Historical repair experiments (`tauceti_queue/repair_experiment.py`, `docs/queue/repair-experiments.md`) | `queue/repair-experiments` | in progress; stacks on A |
| C. Journal report (`scripts/staging_journal_report.py`, `docs/queue/journal-report.md`) | `queue/journal-report` | in progress |
| D. Readmission age in Bors (`queue_age.ex`, handoff record) | bors-ng `queue-age/readmitted-suspects` | in progress |
| GitHub App | operator item 1 | waiting on the operator; registration URL and credential layout in [`operations.md`](operations.md) |
| Dedicated staging runners | operator item 3 | investigation in [staging-runners.md](staging-runners.md); workflow change https://github.com/TauCetiProject/TauCeti/pull/27526 open; Namespace profile and `STAGING_RUNS_ON` waiting on the operator |

## Operator items outstanding

1. Create the `TauCeti Queue` GitHub App from the prefilled URL (`scripts/tauceti_queue_github_app.py url`),
   generate its private key, record both with `scripts/tauceti_queue_github_app.py save`, and install it
   on `TauCetiProject/TauCeti`. The scratch repository can be added to the installation later.
2. Create `TauCetiProject/TauCetiQueueScratch` (needed by stream I, not by increment 0).
3. Reserve dedicated staging capacity: a Namespace runner profile `tauceti-staging` (ubuntu-24.04, 16x32, amd64,
   concurrent runners per profile 3) in the workspace behind the `namespace-managed-runners` App, confirm the
   workspace concurrency limit exceeds `NAMESPACE_BUILD_CAP` (20) by 3 or lower the cap, prove the label is served,
   then set the TauCeti repository variable `STAGING_RUNS_ON` to
   `["namespace-profile-tauceti-staging;container.privileged=true;job.priority=0"]` (exact label from the profile page)
   once https://github.com/TauCetiProject/TauCeti/pull/27526 is merged. Details and the fleet-host alternative in [staging-runners.md](staging-runners.md).
4. Provide the service host (increment 1).

## Latest numbers

None yet. The journal baseline for candidates 1833 through 1932 lands with stream C; the repair
experiment table lands with stream B.

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
| Dedicated staging runners | operator item 3 | placement investigation in progress; request to follow |

## Operator items outstanding

1. Create the `TauCeti Queue` GitHub App from the prefilled URL (`scripts/tauceti_queue_github_app.py url`),
   generate its private key, record both with `scripts/tauceti_queue_github_app.py save`, and install it
   on `TauCetiProject/TauCeti`. The scratch repository can be added to the installation later.
2. Create `TauCetiProject/TauCetiQueueScratch` (needed by stream I, not by increment 0).
3. Provide the service host and reserve dedicated staging runner capacity (increment 1 and the
   increment 0 runner item respectively).

## Latest numbers

None yet. The journal baseline for candidates 1833 through 1932 lands with stream C; the repair
experiment table lands with stream B.

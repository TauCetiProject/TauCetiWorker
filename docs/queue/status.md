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
| Dedicated staging runners | operator item 3 | decided 2026-11-04: staging builds keep running on the fleet hosts as today (no Namespace profile, no dedicated machines, `STAGING_RUNS_ON` stays unset); the placement measurements in [staging-runners.md](staging-runners.md) are the baseline for the pilot |

## Increment 1

Started 2026-11-04 07:50 UTC on the operator's decision to proceed without waiting for the full
experiment sample and without a shadow period; the scratch-repository rehearsal is kept.

| Stream | Branch | State |
| --- | --- | --- |
| E. Controller and state machine (`tauceti_queue/controller.py`, `store.py`, `loop.py`, `ports.py`, `fakes.py`, local end-to-end fixture) | `queue/controller` | in progress |
| F. GitHub adapters and Bors-compatible surface (`github_app.py`, `github_adapter.py`, `commands.py`, `webhook.py`, `feeds.py`, `server.py`) | `queue/github-adapters` | in progress |
| G. Repair task runner (`tauceti_queue/repair.py`, `repair_task.py`) | `queue/repair-task` | in progress; stacks on B |
| H. Deployment (`config.py`, `__main__.py`, `services/queue/`, runbook) | `queue/deployment` | in progress |
| I. Cutover rehearsal | | waits for E to H and the scratch repository |

Storage is one SQLite file on the service host rather than PostgreSQL: a single-process service does
not need a server database, and the operator prefers the simpler host.

## Operator items outstanding

1. Done: the `tauceti-queue` App is created and installed on `TauCetiProject/TauCeti`.
2. Create `TauCetiProject/TauCetiQueueScratch` (stream I).
3. Staging capacity: no action for now (operator decision, 2026-11-04). Revisit from the pilot's measurements.
4. Name the service host (increment 1): a fleet host with the `~/.config/tauceti-queue/` credentials (already on chungus2) and a public HTTPS endpoint `queue.taucetiproject.org` through Cloudflare.

## Latest numbers

Journal baseline (candidates 1833 to 1932, 2026-11-03): 1394 slot minutes, 23 failed large rounds per
7 landed large batches (3.3), 10 conflict and 3 confirmation singletons, 11 handoffs of which 5 were
readmitted (median 51 minutes). Full tables in [journal-report.md](journal-report.md).

Repair experiments (interim, 2026-11-04 05:45 UTC, lane running): 4 of 4 completed candidates green
(1931, 1932, 1930, 1843), agent time 57 s to 175 s, estimated inference cost $0.40 to $0.54 each; the
candidate whose modules were already in the warm pool prepared in 16 s. The sample continues toward
twenty candidates; the table and the warm-artifact decision land in `repair-experiments.md` with
stream B.

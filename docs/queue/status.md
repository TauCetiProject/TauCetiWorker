# AI merge queue: implementation status

Running record for the work plan in
[`docs/redesign/specs/ai-merge-queue.md`](../redesign/specs/ai-merge-queue.md): what is merged,
what is blocked on which operator item, and the latest experiment numbers. Updated by the
orchestrating session as streams land.

## Increment 0

| Stream | Branch | State |
| --- | --- | --- |
| A. Attributed chain tool (`tauceti_queue/chain.py`) | `queue/chain-tool` | merged, https://github.com/TauCetiProject/TauCetiWorker/pull/474 |
| B. Historical repair experiments (`tauceti_queue/repair_experiment.py`, `docs/queue/repair-experiments.md`) | `queue/repair-experiments` | merged (https://github.com/TauCetiProject/TauCetiWorker/pull/477, https://github.com/TauCetiProject/TauCetiWorker/pull/490): 9 of 23 candidates before the disk stop, all green; results and the resume procedure in [repair-experiments.md](repair-experiments.md) |
| C. Journal report (`scripts/staging_journal_report.py`, `docs/queue/journal-report.md`) | `queue/journal-report` | merged, https://github.com/TauCetiProject/TauCetiWorker/pull/475; baseline for candidates 1833 to 1932 in [journal-report.md](journal-report.md) |
| D. Readmission age in Bors (`queue_age.ex`, handoff record) | bors-ng | merged: https://github.com/TauCetiProject/bors-ng/pull/99 (inherit the age once per handoff), https://github.com/TauCetiProject/bors-ng/pull/100 (same head after a lifted conflict request), https://github.com/TauCetiProject/bors-ng/pull/102 (a retired handoff parent no longer blocks the automatic re-offer); deployed and verified live: handoffs on candidates 1948 to 1951 carry the retained age, and PR 26264 (handed off on 1951) was readmitted at 13:38 UTC with a `suspect_readmitted` event and an admission whose `inherited_handoff` names candidate 1951 and whose `original_admitted_at` is its original 01:05 UTC admission, not the readmission time |
| GitHub App | operator item 1 | created: `tauceti-queue`, App ID 5607007, credentials in `~/.config/tauceti-queue/` on chungus2; installed on `TauCetiProject/TauCeti` (installation 178451644), verified with the stored key |
| Dedicated staging runners | operator item 3 | decided 2026-11-04: staging builds keep running on the fleet hosts as today (no Namespace profile, no dedicated machines, `STAGING_RUNS_ON` stays unset); the placement measurements in [staging-runners.md](staging-runners.md) are the baseline for the pilot |

## Increment 1

Started 2026-11-04 07:50 UTC on the operator's decision to proceed without waiting for the full
experiment sample and without a shadow period; the scratch-repository rehearsal is kept.

| Stream | Branch | State |
| --- | --- | --- |
| E. Controller and state machine (`tauceti_queue/controller.py`, `store.py`, `loop.py`, `ports.py`, `fakes.py`, local end-to-end fixture) | `queue/controller` | merged, https://github.com/TauCetiProject/TauCetiWorker/pull/485 and https://github.com/TauCetiProject/TauCetiWorker/pull/486 |
| F. GitHub adapters and Bors-compatible surface (`github_app.py`, `github_adapter.py`, `commands.py`, `eligibility.py`, `webhook.py`, `feeds.py`, `server.py`) | `queue/github-adapters` | merged, https://github.com/TauCetiProject/TauCetiWorker/pull/484 |
| G. Repair task runner (`tauceti_queue/repair.py`, `repair_task.py`) | `queue/repair-task` | merged, https://github.com/TauCetiProject/TauCetiWorker/pull/483 |
| H. Deployment (`config.py`, `__main__.py`, `runtime.py`, `services/queue/`, runbook) | `queue/deployment` | merged, https://github.com/TauCetiProject/TauCetiWorker/pull/482 |
| I. Cutover rehearsal (`tauceti_queue/rehearsal.py`, `scripts/queue-rehearsal`, `docs/queue/rehearsal.md`) | `queue/rehearsal` | merged (https://github.com/TauCetiProject/TauCetiWorker/pull/491); the live rehearsal on `TauCetiProject/TauCetiQueueScratch` passed every scenario the repository can express on 2026-11-04 (clean, eligibility via a `merge eligibility` check run, source failure repaired in place through the real worker runner, shrink to empty, admin advance plus rebase conflict plus re-probe, urgent, stale status, cancelled run, closed member, inference hold, restart, chain validation, feeds, status); report in [rehearsal-live.md](rehearsal-live.md) and [rehearsal-live-rerun.md](rehearsal-live-rerun.md), https://github.com/TauCetiProject/TauCetiWorker/pull/500; it found and fixed the dispatch-ref, log-race and store-cursor defects (https://github.com/TauCetiProject/TauCetiWorker/pull/495, https://github.com/TauCetiProject/TauCetiWorker/pull/497) |

Storage is one SQLite file on the service host rather than PostgreSQL: a single-process service does
not need a server database, and the operator prefers the simpler host.

Cutover shape, from stream F's reading of the workflow callers and TauCetiReview: approvals arrive as
TauCetiReview's `merge eligibility` check run (not `bors r+` comments), which the service consumes as
Bors does; every workflow reader only distinguishes `MERGE_BACKEND == "bors"` from anything else, and
the workers and TauCetiReview read the feeds at `https://bors.taucetiproject.org/...`. Cutover therefore
keeps `MERGE_BACKEND=bors`, suspends the `tau-ceti-bors` App installation, points
`bors.taucetiproject.org` at the service, and enables publication (`docs/queue/github.md`).

## Cutover (2026-11-04)

The service is the production merge backend since 22:58 UTC. Sequence, all times UTC, operator decisions marked:

1. 22:37 to 22:45: tunnel `tauceti-queue` (`be28d750-6092-4526-8aed-76a0d0bf7c66`) serving `queue.taucetiproject.org`, App webhook created on the settings page with all eight events (the registration had left the App without a webhook), first deliveries 202.
2. 22:50: Bors feeds snapshotted (no running head, 22 waiting batches, 1,255 members); backup `queue-20261104T225119Z.sqlite3`; dry run: 8 inputs to import, 1,247 already held by the approval poll.
3. 22:55:55: operator suspended the `tau-ceti-bors` installation (167441900). Bors had dispatched candidate 1984 (batch 2666, 55 members) seconds before; it never reached CI.
4. 22:56: `import-bors` with the fresh feed and the saved journals (`~/tmp/bors-journal/` on tauchonki): 0 new inputs, batch 2666 recorded as the external head, then `--release-head` returned its 55 members to approved inputs with their ages (4 were already members of the open batch).
5. 22:58:06: `publication_enabled = true`, restart, mode `production`. Head 1 (one member, PR 20359) sealed and dispatched at once; open batch 2 held 1,197 members.
6. 23:16: `bors.taucetiproject.org` pointed at the tunnel. The hostname was a Workers Custom Domain of the `tauceti-bors` Worker (Bors runs as a Cloudflare container), which takes precedence over DNS; the custom domain was detached through the API (its definition is saved on chungus2 at `~/tmp/bors-journal/bors-worker-custom-domain-2026-11-04.json`, the rollback value) and the CNAME `bors` to the tunnel answers since. The Worker and its cron keep running against the suspended installation, doing nothing.
7. 23:00 to 23:25: head 1's four staging runs failed before building with "bors identity unavailable": TauCeti's `pr-build.yml` resolves a dispatch through the feed's `train` and fetches the branch `staging-candidate-<candidate>-<generation>`, which the service neither named nor pushed (the scratch repository's workflow lacked that step, so the rehearsal never exercised it). Fixed by https://github.com/TauCetiProject/TauCetiWorker/pull/503, deployed 23:32 as `fc4f5e7b`; `retry-ci` at 23:33; run 41934607574 passed the identity step and built.
8. 23:27: the `merge-queue-main` ruleset's bypass actors and the classic protection's pull-request bypass allowances on `TauCetiProject/TauCeti` gained the queue App (5607007) beside the Bors App (5172678); without it the fast-forward promotion would have been refused.

Compatibility audit after the first failure (every Bors consumer in TauCeti, TauCetiTools at the pinned `8bad7a00`, TauCetiReview, the workers and bors-ng, against the service): the remaining findings and their fixes are

- the service read only the `build` status and ignored `scope` and `bump-guard`, which Bors required (`bors.toml`): fixed by https://github.com/TauCetiProject/TauCetiWorker/pull/509, deployed 00:40 UTC on 2026-11-05;
- TauCetiTools' scope-approval check rejected batches of more than 128 members (Bors's cap): lifted by https://github.com/TauCetiProject/TauCetiTools/pull/166 and pinned by https://github.com/TauCetiProject/TauCeti/pull/28096, both merged 2026-11-05 00:30 UTC (operator decision: no head-size cap in the queue);
- squash commit subjects lacked the ` (#N)` suffix TauCetiTools' lint attribution and GitHub's commit linking use: https://github.com/TauCetiProject/TauCetiWorker/pull/506;
- not blocking, to do: no `tauceti-merge-reconcile` dispatch (TauCetiReview's focused sweep now runs hourly only), no cancellation of a superseded staging run after a rebase, no `tauceti-bors-pr-approved` dispatch after an admin scope approval, and a missing-status run is classified as an infrastructure failure immediately although the finalizer may defer publication for up to an hour.

Head 1's first complete run (23:55 UTC) failed the whole-library environment lint on seven simpNF findings in modules the PR never touched: without the subject suffix the lint could not scope to changed modules, so the findings were inherited from `main`. Rather than send an agent to fix unrelated lints, https://github.com/TauCetiProject/TauCetiWorker/pull/510 added `POST /admin/rebuild-head`, and head 1 was rebuilt with the suffixed subject at 00:41 UTC on 2026-11-05 (version 8, attempt 7).

Also landed or opened on the day: the status summary read the real status document (https://github.com/TauCetiProject/TauCetiWorker/pull/504; the installer's verification had failed on it), the rehearsal repository carries the production staging path and the rehearsal driver scaffolds it with `--public-url` (https://github.com/TauCetiProject/TauCetiWorker/pull/505; hostname `queue-rehearsal.taucetiproject.org` forwards to port 8781 on the service host), and the continuous repair worker unit (https://github.com/TauCetiProject/TauCetiWorker/pull/507; `services/queue/tauceti-queue-repair.service`, `install-repair.sh`, operations.md "Repair worker"), being installed on `chungus` (96 CPUs, 400 GB free) against the production service.

## Operator items outstanding

0. A host for the repair worker and the remaining repair experiments: about 50 GB free, the Lean toolchain, Claude Code credentials, a GitHub login with admin on TauCeti. tauchonki's array is nearly full (staging builds share it) and chungus2 is the operator's development machine.
1. Done: the `tauceti-queue` App is created and installed on `TauCetiProject/TauCeti`.
2. Create `TauCetiProject/TauCetiQueueScratch` (stream I) and add it to the `tauceti-queue` App installation; optionally grant the App `checks: write` so the rehearsal can approve through `merge eligibility` checks (`docs/queue/rehearsal.md`). The rehearsal pushes the content itself.
3. Staging capacity: no action for now (operator decision, 2026-11-04). Revisit from the pilot's measurements.
4. Service host: tauchonki, temporary, in production since 2026-11-04 22:58 UTC ([operations.md](operations.md), "Current deployment"). The dedicated, stable host is still to be named.
5. Done: cutover approved ("go") and executed on 2026-11-04.

## Next steps

- Watch the first three heads land (head 1 is one PR; head 2 is the whole open batch of about 1,200 members, the first large head, with repair tasks run by hand and their transcripts read live until the repair worker host exists).
- Land the verdict-contexts PR and the TauCetiTools pin bump before head 2 reaches its verdict.
- Move the service from tauchonki to a dedicated, stable host (operator condition, 2026-11-04); the procedure is in [operations.md](operations.md), "Moving to the dedicated host".
- Pause the `tauceti-bors` Worker's cron once the operator agrees; delete it with its container after a week of queue operation.
- The `staging-candidate-*` branches the service pushes accumulate as Bors's did; add deletion after the attempt's verdict.
- A second open batch (`open_batch_limit`, default 2; [controller.md](controller.md), "Open batches") is a step beyond increment 1 taken on 2026-11-05 by operator decision, because 189 approved inputs waited behind the 1,197-member open batch with conflicts against its content; watch the rebase conflicts it moves to the agent when the second batch seals.

## Latest numbers

Journal baseline (candidates 1833 to 1932, 2026-11-03): 1394 slot minutes, 23 failed large rounds per
7 landed large batches (3.3), 10 conflict and 3 confirmation singletons, 11 handoffs of which 5 were
readmitted (median 51 minutes). Full tables in [journal-report.md](journal-report.md).

Repair experiments (2026-11-04, lane stopped after 9 of 23 for lack of disk): 9 of 9 completed
candidates green, zero shrink events, every chain validated; time to green (agent plus confirming
build) min 1.1, median 3.3, p75 13.3, max 45.3 minutes against the triage baseline of 19 to 39
minutes plus backlog for the author round-trip; one repair (1916) fixed the three culprits Bors
stripped over three rounds; inference cost $7.48 in total at API list prices (median $0.74, max
$2.17). Warm-artifact decision: `main`'s cache at the base plus an incremental build is warm enough
and is the only source (failed candidates publish no outputs); the cost is about 30 GB of disk per
live base, so the repair worker keeps one pool per head and prunes on base change. Follow-up for the
repair task: hand the agent CI's changed-module list for lint scoping (the two slow runs were lint).

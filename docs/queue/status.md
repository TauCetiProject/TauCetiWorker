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
9. 2026-11-05, 01:10 to 01:13: service moved from tauchonki to chungus. tauchonki's load sat between 150 and 490 (it also runs the fleet's staging builds and many Claude sessions), one controller pass took ten minutes and the public endpoints timed out. Service and backup timer stopped on tauchonki (backup `queue-20261105T011003Z.sqlite3`), `~/TauCetiQueue-data`, the App credentials, `config.toml` and the tunnel files copied, `install.sh --revision d84649118232 --no-restart`, then the service and the tunnel started on chungus as detached processes (no root, no lingering); both hostnames report host chungus since 01:13:33. tauchonki's units are disabled and its data directory kept for a day.
10. 2026-11-05, 00:33: head 1 landed (00:33:19) and head 2 (batch 2, 1,197 members) sealed (00:33:23). Its scripted rebase onto `main` conflicted at PR 24255 and became rebase-conflict task 2.
11. 2026-11-05, 03:13: the repair worker on chungus claimed task 2 for real, after the plumbing defects listed under "First repair run" were fixed.

Compatibility audit after the first failure (every Bors consumer in TauCeti, TauCetiTools at the pinned `8bad7a00`, TauCetiReview, the workers and bors-ng, against the service): the remaining findings and their fixes are

- the service read only the `build` status and ignored `scope` and `bump-guard`, which Bors required (`bors.toml`): fixed by https://github.com/TauCetiProject/TauCetiWorker/pull/509, deployed 00:40 UTC on 2026-11-05;
- TauCetiTools' scope-approval check rejected batches of more than 128 members (Bors's cap): lifted by https://github.com/TauCetiProject/TauCetiTools/pull/166 and pinned by https://github.com/TauCetiProject/TauCeti/pull/28096, both merged 2026-11-05 00:30 UTC (operator decision: no head-size cap in the queue);
- squash commit subjects lacked the ` (#N)` suffix TauCetiTools' lint attribution and GitHub's commit linking use: https://github.com/TauCetiProject/TauCetiWorker/pull/506;
- not blocking, to do: no `tauceti-merge-reconcile` dispatch (TauCetiReview's focused sweep now runs hourly only), no cancellation of a superseded staging run after a rebase, no `tauceti-bors-pr-approved` dispatch after an admin scope approval, and a missing-status run is classified as an infrastructure failure immediately although the finalizer may defer publication for up to an hour.

Head 1's first complete run (23:55 UTC) failed the whole-library environment lint on seven simpNF findings in modules the PR never touched: without the subject suffix the lint could not scope to changed modules, so the findings were inherited from `main`. Rather than send an agent to fix unrelated lints, https://github.com/TauCetiProject/TauCetiWorker/pull/510 added `POST /admin/rebuild-head`, and head 1 was rebuilt with the suffixed subject at about 00:17 UTC on 2026-11-05 (version 8, attempt 7, run 41940173478).

Also landed or opened on the day: the status summary read the real status document (https://github.com/TauCetiProject/TauCetiWorker/pull/504; the installer's verification had failed on it), the rehearsal repository carries the production staging path and the rehearsal driver scaffolds it with `--public-url` (https://github.com/TauCetiProject/TauCetiWorker/pull/505; hostname `queue-rehearsal.taucetiproject.org` forwards to port 8781 on the service host), and the continuous repair worker unit (https://github.com/TauCetiProject/TauCetiWorker/pull/507; `services/queue/tauceti-queue-repair.service`, `install-repair.sh`, operations.md "Repair worker"), running on `chungus` against the production service since 2026-11-05.

Found 2026-11-05 03:20 UTC: 94 of the 1,197 members of open batch 2 had been landed by Bors during the shadow period with exactly the head the batch holds, and the scripted rebase of the head conflicted at its 14th commit because landed and unlanded changes interleave in the same files. Members are never withdrawn; since https://github.com/TauCetiProject/TauCetiWorker/pull/526 the chain tool recognises a member the new base already carries (subject ` (#N)` or `Source-PR` trailer in the commits since the old base) and writes an empty commit with a note instead of cherry-picking it, and the rebase-conflict diagnostics list such members under `landed_on_base`.

## First repair run (head 2, task 2)

Defects found between the seal and the claim, each fixed before the worker could start:

- refs retained during shadow mode were not republished: https://github.com/TauCetiProject/TauCetiWorker/pull/516;
- claim release and resume across restarts, batched ref fetches: https://github.com/TauCetiProject/TauCetiWorker/pull/518, https://github.com/TauCetiProject/TauCetiWorker/pull/520, https://github.com/TauCetiProject/TauCetiWorker/pull/524;
- artifacts prepared for the rebase target, and the stale cache log: https://github.com/TauCetiProject/TauCetiWorker/pull/521, https://github.com/TauCetiProject/TauCetiWorker/pull/529;
- the 291 KB prompt exceeded the argument limit and now goes on stdin: https://github.com/TauCetiProject/TauCetiWorker/pull/522;
- a short prompt, rewritten after operator review: https://github.com/TauCetiProject/TauCetiWorker/pull/523, https://github.com/TauCetiProject/TauCetiWorker/pull/525;
- the detached processes ran with a file-descriptor limit of 1,024: raised to 65,536 in the supervisor scripts;
- the Claude Code binary on PATH was too old for the model: the managed install was updated to 2.1.318 and the PATH order fixed.

The agent (Claude Code 2.1.316, `claude-opus-5-5`, effort high, Max subscription) finished the rebase of all 1,197 commits by about 03:50 UTC: the 94 members Bors had landed during shadow became empty commits and 36 messages were extended. It then repaired the first build pass's 42 failing modules (duplicate declarations between members, rename ripples such as PR 25195's Killing-form API, a missing instance, API changes on `main`), committing each fix into the owning member. At about 05:00 UTC it was in the second pass, fixing dependents and sweeping cross-module duplicates, after more than 950 tool calls.

## Repair improvements (2026-11-05)

Operator decisions recorded in the plan ([ai-merge-queue.md](../redesign/specs/ai-merge-queue.md)): no head-size cap, and the five improvements from the first repair plus items 6 and 7 implemented at once. All merged on 2026-11-05:

- file ownership and declaration context for the agent: https://github.com/TauCetiProject/TauCetiWorker/pull/532;
- repair while the build streams: https://github.com/TauCetiProject/TauCetiWorker/pull/531;
- artifacts from the newest cached revision: https://github.com/TauCetiProject/TauCetiWorker/pull/529;
- parallel agents within a task, orchestrated by the worker and provider-neutral: https://github.com/TauCetiProject/TauCetiWorker/pull/534;
- a scoped build helper (`.tauceti-repair/build`): https://github.com/TauCetiProject/TauCetiWorker/pull/528;
- repair outputs published to the worker cache `tauceti-repair` (R2 bucket created, `repair-cache.taucetiproject.org` attached, write credential not yet created): https://github.com/TauCetiProject/TauCetiWorker/pull/530;
- repair builds in CI's `bwrap` sandbox: https://github.com/TauCetiProject/TauCetiWorker/pull/533;
- provider chain `claude`, `codex` (`gpt-6.1-sol`), `api`, admitted by the fleet's pace curve: https://github.com/TauCetiProject/TauCetiWorker/pull/527;
- members already on `main` replayed as empty commits automatically: https://github.com/TauCetiProject/TauCetiWorker/pull/526;
- worker and operator identities cached through GitHub rate limits: https://github.com/TauCetiProject/TauCetiWorker/pull/519;
- bounded controller passes, batched ref pushes and a status snapshot: https://github.com/TauCetiProject/TauCetiWorker/pull/515;
- a second open batch: https://github.com/TauCetiProject/TauCetiWorker/pull/514.

Undecided: whether CI may consume the repair's published outputs instead of recompiling (candidate: a `lean4checker` replay in CI over the published oleans); to be decided after head 2 lands.

## Operator items outstanding

0. Repair worker host: chungus for now (96 CPUs, 400 GB free), with the Claude Code credentials and the API key the operator placed there; it moves with the service to the dedicated host.
6. Create the R2 write token for the `tauceti-repair` bucket and place it on the repair host as `TAUCETI_REPAIR_CACHE_KEY_FILE`; until then repairs read the worker cache but publish nothing.
7. Agree to pausing the `tauceti-bors` Worker's cron (it still runs against the suspended installation).
1. Done: the `tauceti-queue` App is created and installed on `TauCetiProject/TauCeti`.
2. Create `TauCetiProject/TauCetiQueueScratch` (stream I) and add it to the `tauceti-queue` App installation; optionally grant the App `checks: write` so the rehearsal can approve through `merge eligibility` checks (`docs/queue/rehearsal.md`). The rehearsal pushes the content itself.
3. Staging capacity: no action for now (operator decision, 2026-11-04). Revisit from the pilot's measurements.
4. Service host: chungus since 2026-11-05 01:13 UTC, temporary (moved from tauchonki, whose staging builds and Claude sessions held its load between 150 and 490 and stretched one controller pass to ten minutes). No root and no lingering there, so the service, the tunnel and the repair worker run as detached processes started by hand, with no restart after a reboot and no backup timer ([operations.md](operations.md), "Current deployment" and "Hosts without lingering"). A fresh dedicated machine is expected about eight hours after the move.
5. Done: cutover approved ("go") and executed on 2026-11-04.

## Next steps

- Let task 2 finish and submit, then watch head 2's CI round and landing.
- After the current run submits, deploy the merged worker and service code on chungus (both were deliberately not restarted mid-run) and verify the running revisions.
- Move the service and the repair worker from chungus to the dedicated machine when it arrives, restoring the systemd units and the backup timer ([operations.md](operations.md), "Moving to the dedicated host").
- Pause the `tauceti-bors` Worker's cron once the operator agrees; delete it with its container after a week of queue operation.
- Give Bors's admission-time checks (declaration collisions, merge-selection overlaps) to the agent as diagnostics, as described in the plan; streaming repair, the other follow-up there, is done (https://github.com/TauCetiProject/TauCetiWorker/pull/531).
- The `staging-candidate-*` branches the service pushes accumulate as Bors's did; add deletion after the attempt's verdict.
- Decide whether PRs that modify `scripts/` or `.github/` should enter batches at all: the repair
  agent is told not to introduce changes there, but a constituent PR that modifies such files keeps
  its change, so a batch can still land them.
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

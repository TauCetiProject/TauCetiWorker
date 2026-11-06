# AI merge queue: implementation status

Running record for the work plan in
[`docs/redesign/specs/ai-merge-queue.md`](../redesign/specs/ai-merge-queue.md): what is merged,
what is blocked on which operator item, and the latest experiment numbers. Updated by the
orchestrating session as streams land.

## Primary admission cutover (2026-11-06)

The queue service became the authoritative automatic admission owner at 04:25 UTC, through
[Worker PR 572](https://github.com/TauCetiProject/TauCetiWorker/pull/572). It evaluates the pure
policy pinned at TauCetiReview `7fd3a39e02c5`, using the existing approval-poll thread and SQLite
database. [Worker PR 577](https://github.com/TauCetiProject/TauCetiWorker/pull/577) fixes shared
resource waits, FIFO progress for whole evaluations, resumed label pagination, and invalidations
from the queue's own output. All nine final CI jobs passed; merged revision `bb451e63e5a0` was
deployed live at 05:55:01 UTC. Both public hostnames reported that revision.

[TauCeti PR 29458](https://github.com/TauCetiProject/TauCeti/pull/29458) retired the separate
auto-merge, merge-sweep and queue-release workflows and the control-batch merge caller. The local
workflow backstop no longer dispatches admission workflows. Late legacy eligibility publications
have no authority in policy mode. Normal review publication, worker admissions and CI remain
enabled. Inference failure keeps work in this engine under the existing hold/retry behavior;
inference-free splitting remains a deferred TODO.

At 05:55:34 UTC, the service had recorded 263 admissions and 1,023 evaluations; 1,120 evaluations
remained pending, down from 1,496 shortly after cutover. Independently audited PRs 28216, 28688
and 28675 were all inserted with their exact accepted heads. Deployment preserved all 1,803
existing membership records and runtime settings, and SQLite `quick_check` passed. These are
point-in-time observations, not a full authorization audit of the ready-labelled backlog.

The remaining deployment gap is boot supervision on chungus: `Linger=no`, enabling it is denied,
and the service unit remains disabled while the detached supervisor owns the running process.
The supervisor recovers process crashes but cannot start after a host reboot. The backup timer
is enabled and active, with a successful November 6 run, but has the same user-session limitation.
This host constraint does not prevent normal supervision by the running user manager; it remains
relevant to reboot startup. See [the operations runbook](operations.md#current-deployment).

## Repair memory-pressure incident (2026-11-06)

At 05:47:40 UTC, `systemd-oomd` killed `session-1994.scope`, containing both repair workers and
their shell supervisors. The journal records memory pressure of 98.04% for over 30 seconds; the
session reached 50 GB of memory and 14.7 GB of swap. Both builds stopped, claims expired around
05:58, and head 34's 141 PRs had no repair worker. Restarting the same shell loops at 06:55
repeated the failure. `nohup` did not move the supervisors out of the killable login scope.

Repair builds also bypassed the existing compiler pressure gate, and their sandbox hid capped
cgroup ancestors. The account's existing limit is approximately 50.2 GiB, below host RAM. Builds
now reuse the trusted bundled gate, with cgroups visible read-only and their namespace retained;
there is no new memory cap. A real sandbox compilation and ancestor-visibility test passed, and
the guard correctly waited under measured 94% account memory use.

At 07:04 UTC both workers moved to separate systemd user services outside the login scope, with
native restart and whole-worker OOM policy. A local full-cgroup SIGKILL fixture verified a new
worker process resumed the same claim/session and produced one submission. The existing status
path now exposes unclaimed head tasks after the configured stall interval; previously it only
covered CI silence, which left this failure invisible while admissions continued. Normal worker
and CI admissions remained enabled throughout. Live revisions and subsequent progress must be
checked through status and the two repair journals.

The scheduler's former fallback allowed a spare worker to race an already-owned head task when
no background task was claimable. This duplicated preparation builds and inference. The operator
rejected that behavior on November 6: tasks now have one live owner, while a second worker takes
distinct next-batch work or waits. The runtime concurrency setting still permits concurrent work
on separate tasks; it is not a per-task replication count.

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

## Head 2, second round (2026-11-05)

All times UTC. CI attempt 9 (run 41986292089) failed at 09:01 on `build` alone (`bump-guard` and `scope` passed), with one audit finding: `duplicate-declarations: SimpleGraph.Walk.IsPath.start_notMem_tail_support is declared in both TauCeti.Combinatorics.SimpleGraph.Connectivity.Menger and TauCeti.Combinatorics.SimpleGraph.Paths`.

Repair task 5, claimed by worker 1 at 09:08:

- the streaming build was green at 09:11:53 (66,740 jobs);
- two parallel groups each deleted one copy of the lemma (PR 24945's copy in Menger, PR 27672's copy in Paths), and the fold's verification build failed at 09:14:55;
- the lead agent restored the lemma in `Paths.lean` within PR 27672's commit and finished at 09:18:58 (16 turns, about 3 minutes); the worker ran no verification build of its own after the lead agent (fixed by https://github.com/TauCetiProject/TauCetiWorker/pull/545);
- publishing the outputs (25,918 artifacts, 734 MiB) took 990 s and ran before submission; the chain was submitted at 09:36:18 as version 1200, tip `89293a361f`. CI attempt 10 is run 42003719651.

Lessons: the worker must run CI's audits itself, since a green build is not a green CI (https://github.com/TauCetiProject/TauCetiWorker/pull/545), and publishing must not delay submission (https://github.com/TauCetiProject/TauCetiWorker/pull/548, merged later the same day).

Batch 3 (207 members): pre-rebase task 3 adopted at 09:08:26; prepare task 4 claimed by worker 2 at 09:13:53, still running at the time of writing.

## Settings regression (2026-11-05)

The systemd user unit `tauceti-queue.service` on chungus was still enabled after the hand-started supervisor took over, and sat in an auto-restart loop (about every 6 s, 737 restarts). Each attempt seeded the config file's `[settings]` into the shared store before failing to bind port 8780, which reverted the live `repair_concurrency` from 2 (set by the operator at 06:06) to 1: worker 2 was refused every claim (`concurrency`) from 07:04 to 09:13. The settings journal gained about 3,000 rows per hour from 06:27, and also from 01:14 to about 01:30.

Remedy: `systemctl --user stop tauceti-queue.service` and `systemctl --user disable tauceti-queue.service` at 09:24; the config file now says `repair_concurrency = 2` (backup `config.toml.bak-2026-11-05`); https://github.com/TauCetiProject/TauCetiWorker/pull/544 makes the file's settings seeds only and stops journaling unchanged values. Guidance in [operations.md](operations.md), "Current deployment".

## Restarts and main's tests (2026-11-05)

- The service and worker 1 were restarted onto `9d2643f` (https://github.com/TauCetiProject/TauCetiWorker/pull/544, https://github.com/TauCetiProject/TauCetiWorker/pull/545) between 09:38 and 09:41; worker 2 is to be restarted after task 4. The procedure's pitfalls (the baked revision after a plain `git pull`, restarting one worker by PID) are in [operations.md](operations.md), "Hosts without lingering".
- `main`'s `test (portable)` job was red from 05:54, with failures stacked by https://github.com/TauCetiProject/TauCetiWorker/pull/531, https://github.com/TauCetiProject/TauCetiWorker/pull/527, https://github.com/TauCetiProject/TauCetiWorker/pull/514 and https://github.com/TauCetiProject/TauCetiWorker/pull/522; https://github.com/TauCetiProject/TauCetiWorker/pull/544 and https://github.com/TauCetiProject/TauCetiWorker/pull/545 were admin-merged with only those failures, and https://github.com/TauCetiProject/TauCetiWorker/pull/546 fixes them.

## Merged after the first repair run (2026-11-05)

- open batches built and repaired in the background: https://github.com/TauCetiProject/TauCetiWorker/pull/536;
- the next batch pre-rebased once the head's chain is submitted: https://github.com/TauCetiProject/TauCetiWorker/pull/537;
- a batch prepared before it is pre-rebased, one background task per batch: https://github.com/TauCetiProject/TauCetiWorker/pull/538;
- the repair worker's admin check cached across rate limits: https://github.com/TauCetiProject/TauCetiWorker/pull/539;
- edit summaries, the opt-out label and `problem-anticipated`: https://github.com/TauCetiProject/TauCetiWorker/pull/540;
- an in-progress CI run never treated as lost, live runs re-adopted: https://github.com/TauCetiProject/TauCetiWorker/pull/541;
- the opt-out label renamed to `dont-repair-in-queue`: https://github.com/TauCetiProject/TauCetiWorker/pull/542;
- the parallel repair test made deterministic: https://github.com/TauCetiProject/TauCetiWorker/pull/543;
- configuration settings seed the store instead of overriding live values: https://github.com/TauCetiProject/TauCetiWorker/pull/544;
- CI's audits and changed-module lint run in the repair worker: https://github.com/TauCetiProject/TauCetiWorker/pull/545;
- the portable tests green without bubblewrap: https://github.com/TauCetiProject/TauCetiWorker/pull/546.

## Head 2 landing (2026-11-05)

All times UTC. Head 2 landed at about 14:09 on CI attempt 13 (run 42037197161): 1,197 PRs, `main` now `482d8ed159aba195ef10855ef14986d48f2277c4`. The rounds after attempt 10:

- attempt 10 (run 42003719651) was cancelled by the replay when https://github.com/TauCetiProject/TauCeti/pull/28186 merged at 10:59;
- attempt 11 (run 42015500147), dispatched at 11:01, had only the base as fetch candidate because the new `batch_tips` table was empty; the operator backfilled the tips and cancelled it at 11:04;
- attempt 12 (run 42016336692) ran from 11:05 to 12:52, built from scratch and failed in `lint-env` with 8 new simpNF violations (the audits passed);
- repair task 10 (worker 1, Codex `gpt-6-astra`; the Claude subscription was skipped as ahead of pace) was adopted at 13:34:57 as tip `1a5349d2`, and the replay onto `main` `b3b0334a` (https://github.com/TauCetiProject/TauCeti/pull/28264) gave version 1203, tip `482d8ed1`;
- attempt 13 was dispatched at 13:35 with fetch candidates `1a5349d2`, `3477e48a`, `89293a36`, `6d0442e5` and `b3b0334a`; fetching the repair outputs took 7 minutes (13:43 to 13:50) and the build, audits and lint 18.5 minutes (13:50 to 14:09), and it succeeded.

The outbox then held 2,394 landed comments and closes to drain.

## Cache findings (2026-11-05)

The rounds took about three hours because CI's base-cache lookup walks at most 1,000 first-parent ancestors, and a 1,197-commit head puts `main` beyond that walk: CI discarded the base cache and compiled everything from source, and the repair-output fetch then skipped itself because `LAKE_ARTIFACT_CACHE=false`. Fixes, all merged on 2026-11-05:

- TauCetiTools: several candidate revisions (https://github.com/TauCetiProject/TauCetiTools/pull/168), the fetch even when the base cache was discarded and the miss regex (https://github.com/TauCetiProject/TauCetiTools/pull/169), and the exact base lookup `LAKE_CACHE_BASE_REV` (https://github.com/TauCetiProject/TauCetiTools/pull/170);
- TauCeti: CI consumes the repair outputs (https://github.com/TauCetiProject/TauCeti/pull/28186, merged at 10:59 as `d0b6fe9d`), and the pin bump with the base revision (https://github.com/TauCetiProject/TauCeti/pull/28264, merged between 13:20 and 13:30 as `b3b0334a`);
- TauCetiWorker: `repair_outputs_revs` in the dispatch payload and the `batch_tips` table (https://github.com/TauCetiProject/TauCetiWorker/pull/550); publishing after submission (https://github.com/TauCetiProject/TauCetiWorker/pull/548); the pre-rebase warmed and prepared from the repair cache (https://github.com/TauCetiProject/TauCetiWorker/pull/553); head repairs warmed from the head's published tips (https://github.com/TauCetiProject/TauCetiWorker/pull/554); the pre-rebased chain prepared (https://github.com/TauCetiProject/TauCetiWorker/pull/551).

Incidents on the way:

- The fleet harness's "Build cache preparation" prompt appendix in `tauceti_worker/agents.py` told the Codex agent to stop on a public-cache miss, which aborted batch 3's pre-rebase twice; fixed with https://github.com/TauCetiProject/TauCetiWorker/pull/553.
- The new `ci_attempts` column from https://github.com/TauCetiProject/TauCetiWorker/pull/550 was not added to the live store, and status failed for four minutes from 10:33 until the operator ran the `ALTER TABLE`; https://github.com/TauCetiProject/TauCetiWorker/pull/552 derives the column migration from the schema.
- From 12:40 to 12:51 a service restart during GitHub rate limiting left both workers refused with 401, and one claim expired; https://github.com/TauCetiProject/TauCetiWorker/pull/555 persists the identities across restarts.

## Batch 3 (2026-11-05)

Batch 3 held 234 PRs after insertions. Its own-base prepare task 8 was adopted at 12:25; pre-rebase tasks 6, 7 and 9 were each superseded by a change of head 2; task 11, onto `482d8ed1`, was running on worker 1 at 13:36.

## Provider chain in production (2026-11-05)

The Claude subscription was skipped all day as ahead of its weekly pace (55% used at 33% of the week). Codex through the ChatGPT account first failed on `gpt-6.1-sol` with `model_not_found` ("The 'gpt-6.1-sol' model is not supported when using Codex with a ChatGPT account"), and the repair environment was pinned to `TAUCETI_AUTHORING_CODEX_MODEL=gpt-6-astra` as a workaround. The cause was the Codex CLI on chungus, 0.154.0, which lacks the `gpt-6.1-sol` model metadata ("Model metadata for `gpt-6.1-sol` not found"), not the account. After `npm install -g @openai/codex@latest` (0.166.0) at 20:50 UTC, `codex exec --model gpt-6.1-sol` on the same Pro ChatGPT account answered normally; the pin was removed and both workers restarted, so the default Sol profile with its probe and `gpt-6-luna` fallback is in use. A `model_not_found` for Sol on a ChatGPT account means an outdated Codex CLI: update it, and compare `codex --version` with another fleet host's when checking a host.

## Heads 3 and 4 landing (2026-11-05)

All times UTC.

- Head 3 (batch 3, 237 PRs after insertions) sealed at 15:27:24 on tip `99310cef` (version 239) and was dispatched as attempt 14 (run 42055229481) with eight fetch candidates, among them head 2's published tips `1a5349d2` and `89293a36` (https://github.com/TauCetiProject/TauCetiWorker/pull/557, admin-merged and deployed at 14:27, before the seal); the repair-output fetch took 9.5 minutes (15:37:34 to 15:47:07).
- An unrelated landing moved `main` under it (base `83b84968`), so the controller replayed it to tip `28b669f8` and dispatched attempt 15 at 15:37:42; that run passed and head 3 landed at 17:01:27 by promotion.
- The superseded run 42055229481 kept running until the operator cancelled it at 17:03: the workflow's concurrency group did not displace it, to be investigated.
- Head 4 (batch 4, 9 PRs) was the first batch to go through the pre-rebased prepare of https://github.com/TauCetiProject/TauCetiWorker/pull/551: task 13 on worker 2 (Claude agent at 16:41:03, adopted at 16:41:31, outputs for tip `946de93f` published in the background). It sealed at 17:01:33 on that exact tip, was dispatched as attempt 16 (run 42070564972) and landed at 17:35:15; the whole round took 34 minutes.

Totals for the day: four heads landed through the queue (head 1 at 00:33, head 2 with 1,197 PRs at 14:09, head 3 with 237 at 17:01, head 4 with 9 at 17:35), 1,444 PRs in all. At 17:40 no approved PR was waiting, the outbox was empty, and two new approvals had entered the next open batch.

## Afternoon deploys (2026-11-05)

- The service ran `64dadc13` from 14:27 (https://github.com/TauCetiProject/TauCetiWorker/pull/557) and `ac2f078d` from 15:12 (https://github.com/TauCetiProject/TauCetiWorker/pull/558: worker endpoints prioritised over the tick, and the outbox's GitHub calls and the insertion probes taken off the controller lock).
- Between 14:21 and 14:52 worker renewals and claims timed out behind the tick while the outbox drained about 2,400 landing comments and closes. The operator lowered `outbox_drain_budget_seconds` and `insertion_probe_budget_seconds` to 5 through the admin endpoint (the CLI itself timed out), and restored 20 after https://github.com/TauCetiProject/TauCetiWorker/pull/558 deployed.

## Operator items outstanding

0. Repair worker host: chungus for now (96 CPUs, 400 GB free), with the Claude Code credentials and the API key the operator placed there; it moves with the service to the dedicated host.
6. Create the R2 write token for the `tauceti-repair` bucket and place it on the repair host as `TAUCETI_REPAIR_CACHE_KEY_FILE`; until then repairs read the worker cache but publish nothing.
7. Agree to pausing the `tauceti-bors` Worker's cron (it still runs against the suspended installation).
1. Done: the `tauceti-queue` App is created and installed on `TauCetiProject/TauCeti`.
2. Create `TauCetiProject/TauCetiQueueScratch` (stream I) and add it to the `tauceti-queue` App installation; optionally grant the App `checks: write` so the rehearsal can approve through `merge eligibility` checks (`docs/queue/rehearsal.md`). The rehearsal pushes the content itself.
3. Staging capacity: no action for now (operator decision, 2026-11-04). Revisit from the pilot's measurements.
4. Service host: chungus since 2026-11-05 01:13 UTC, temporary (moved from tauchonki, whose staging builds and Claude sessions held its load between 150 and 490 and stretched one controller pass to ten minutes). No root and no lingering there. The queue and tunnel retain detached supervisors; both repair workers use separate systemd user services with OOM recovery since November 6 at 07:04. Automatic startup after reboot remains unavailable. The backup timer is active but also depends on the user manager ([operations.md](operations.md), "Current deployment" and "Hosts without lingering"). The dedicated host's availability and hostname remain outstanding.
5. Done: cutover approved ("go") and executed on 2026-11-04.

## Next steps

- Watch new approvals flow through the queue.
- Investigate why the concurrency group did not cancel the superseded run 42055229481.
- The Zulip reply to Christian still awaits the operator.
- The fresh machine for the service (below).
- Read the current service revision from `/status`; use the live deployment procedure in the runbook.
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


## Admission evidence and pilot measurements (2026-11-05)

Implementation is tracked by [PR 563](https://github.com/TauCetiProject/TauCetiWorker/pull/563)
(compiled PR-build declaration records), [PR 564](https://github.com/TauCetiProject/TauCetiWorker/pull/564)
(attempt-bound journal reporting and the pilot report), and
[PR 565](https://github.com/TauCetiProject/TauCetiWorker/pull/565)
(merge-selection evidence and proactive repairs). The compiled-record fetch is activated by PR 565;
563 and 565 deploy together. The operating contract is in [admission-knowledge.md](admission-knowledge.md).

The first [pilot report](pilot-2026-11-05.md), through 21:35:59 UTC, separates 1,506 member landings,
1,502 unique PR identities, 1,401 nonempty contributions and 105 empty commits. Rates are 66.37
unique PR identities/hour and 61.91 nonempty contributions/hour; the comparable Bors identity rate
is 17.97/hour, with nonempty baseline contributions unknown. The inherited backlog makes this an
initial observation, not a sustained-throughput result. Head 2 took 494.23 minutes from first
dispatch to landing, so the 120-minute scenario was not met in the observed data; a full week
cannot yet be assessed. Recorded cost estimates total $157.51, with 30 provider attempts missing
estimates and total spend explicitly unknown. [The journal runbook](journal-report.md) gives the
read-only snapshot and daily-report commands.

Production audits on the same day:

- All eleven landed chains through batch 14 (1,508 member occurrences) passed the chain validator:
  single-parent history, exact recorded base and a bijection to accepted PR identities/heads.
- All 268 edited-member landing comments had exactly one queue comment, a retained
  commit link and their appended explanation. Four (26346, 26540, 27033, 27785) explicitly reported
  no difference in the PR's files; clean-replay suppression remains a later increment.
- Recent promotions' `build`, `scope` and `bump-guard` statuses were all bound to their own completed
  run. The sampled status had no conditions, inference hold, active claims or pending outbox entries.
- An online SQLite backup passed integrity checking at 21:36:50 UTC. The full comment audit is at
  `~/TauCetiQueue-data/reports/2026-11-05/comment-audit.json` on chungus; report inputs and script
  copies are archived under `~/TauCetiQueue-data/reports/` there. These are observations at the
  recorded times; deployment verification must recheck live status.

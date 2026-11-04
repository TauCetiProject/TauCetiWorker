# Dedicated staging runners

Investigation for the increment 0 item "Dedicated staging runners" of the
[AI merge queue plan](../redesign/specs/ai-merge-queue.md), 2026-11-04. The workflow change it
proposes is https://github.com/TauCetiProject/TauCeti/pull/27526; the operator steps are in the request at the end and in [status.md](status.md).

## 1. How Bors dispatches a staging build, and where it lands

**Dispatch.** Bors does not push to a `staging` branch that a workflow watches. For the configured project
(`BORS_STAGE_DISPATCH_PROJECT`) it force-pushes the candidate commit to a `staging-candidate-<id>-<gen>` ref and then
calls the GitHub `repository_dispatch` API:

- `/tmp/bors-ng-increment0/lib/github/github/server.ex:363-388`: `POST /repos/TauCetiProject/TauCeti/dispatches` with
  `event_type: "tauceti-bors-staging"` and `client_payload: {head_sha, base_sha, batch_id, members, base_ref}`.
- `/tmp/bors-ng-increment0/lib/worker/batcher.ex:3212-3236` journals `dispatch_requested` (`bors.workflow_dispatch` /
  `bors.outbox`) then `dispatched` with source `github.workflow_dispatch_response`. The source name is historical; the
  call is `repository_dispatch`.
- Bors finds and caps its runs by listing `actions/workflows/pr-build.yml/runs?event=repository_dispatch` and matching
  `display_title` = `bors branch=<b> batch=<id> head=<sha> base=<sha>` (`server.ex:425-497`). `active_staging_count`
  refuses a new dispatch when 3 or more are active (`batcher.ex:3064-3067`), so Bors itself never has more than 3
  staging builds in flight.

**Workflow.** `TauCeti/.github/workflows/pr-build.yml` on `main` (commit `6c781b0a73`, 1602 lines; the checkout at
`/home/kim/TauCeti` is Sep 17 and does not have this) has `on.repository_dispatch.types:
[tauceti-bors-staging, tauceti-bors-pr-approved]` (line 36). The `run-name` (line 2) builds the `bors branch=...`
title Bors matches on. The build job is `sandboxed-build`; `finalize-merge-group-build` posts the required `build`
status; `publish-merge-group-cache` uploads oleans.

**Runner selection.** `sandboxed-build.runs-on` (line 99) is an expression:

1. maintenance / bot bump builds: Namespace `nscloud-ubuntu-24.04-amd64-8x16-with-features;job.priority=1`;
2. otherwise the output of the reusable `pick-runner.yml` job, if its `attempt` matches the current attempt;
3. otherwise GitHub-hosted `ubuntu-24.04`.

`pick-runner` is called with `fleet-staging: true` for Bors dispatches (line 78) and runs
`scripts/fleet_runner.py --route` from TauCetiTools
(`/tmp/staging-runner/TauCetiTools/tauceti_tools/assets/scripts/fleet_runner.py`). It walks
`[services.fleet].staging_order` from `TauCeti/tauceti.toml` (line 70):

```
staging_order = ["tauchonki", "chris", "yael", "brett", "namespace", "github"]
pr_order      = ["github", "chris", "tauchonki", "yael", "brett", "namespace"]
```

For each fleet machine it looks for an online, idle runner in GitHub runner group `TauCetiFleet` (org variable
`FLEET_RUNNER_GROUP_ID=3`, `FLEET_CI_ENABLED=true`) carrying labels `self-hosted, linux, x64, fleet-<machine_id>,
tauceti-fleet-staging, fleet-instance-<16 hex>` (`fleet_runner.py:40-70`) and returns exactly those labels, so the job
is bound to one guest VM on one host. Only if no fleet guest is idle does it fall to Namespace
(`nscloud-ubuntu-24.04-amd64-16x32-with-features;job.priority=1` plus
`namespace-features:container.privileged=true;github.run-id=<run>`, `fleet_runner.py:465-467`, limited by
`NAMESPACE_BUILD_CAP=20`), then GitHub-hosted (`HOSTED_RUNNER_BUDGET=12`).

**So yes: the staging path lands on fleet hosts by preference, not by accident.** Fleet hosts are the same machines
that run worker agents (TauCetiFleet README: CI slice CPUWeight 1000 vs worker slice 10, "I/O and memory-bandwidth
isolation are not provided"), and they also take PR builds (`pr_order` lists the same four machines). In the 14-day
window 668 of 999 staging build attempts (67%) ran on fleet guests, 252 (25%) on Namespace 16x32, 79 (8%) on
GitHub-hosted `ubuntu-24.04`. GitHub's native merge queue (`merge_group`) produced no builds in the window.

**Where the 2h50 comes from.** Bors journal (live feed `https://bors.taucetiproject.org/repositories/1/candidates`,
saved at `data/feed_repo1_candidates.json`): candidate 1934, batch 2629, 108 member PRs, `dispatched` at
2026-11-03T23:30:43Z, next event `status_observed` (bump-guard ok) at 2026-11-04T02:17:01Z, 2h46m later with no
intermediate information. The journal's `runner_assigned_at/started_at/finished_at` fields are null for all 100
retained candidates; placement is not journaled today.

Run https://github.com/TauCetiProject/TauCeti/actions/runs/41739726184 (`bors branch=main batch=2629
head=8e29268de7 base=333c9e4914`):

| job | runner | labels | started | completed | duration |
|---|---|---|---|---|---|
| pick-runner / pick | `nsc-runner-j5udtihe565vk` | `nscloud-...-2x4-with-features;job.priority=0` | 23:30:58 | 23:31:08 | 10 s |
| sandboxed-build | `fleet-11e2756fc110-c96ca537c9b86c28`, group `taucetifleet` | `fleet-11e2756fc11042d798f759410573a935, fleet-instance-c96ca537c9b86c28, linux, self-hosted, tauceti-fleet-staging, x64` | 23:31:09 | 02:17:07 | 2h46m |
| finalize-merge-group-build | `nsc-runner-4oqpo7n0arvhu` | Namespace 2x4 | 02:17:19 | 02:20:42 | 3 min |

Machine `11e2756f...` is `tauchonki` (`tauceti.toml:76`). Queue wait was 1 second: the build was slow, not queued.
Step timings: "Build the trusted environment-lint driver" 50.6 min (23:49:46 to 00:40:24; the same step takes 6 to 12
seconds on an unloaded fleet guest, e.g. run 41737087285), "Build exact candidate under bwrap" 94.8 min (00:40:24 to
02:15:11). During that window the same host ran 20 other `sandboxed-build` jobs for PR builds on its guests
(`data/pr_jobs_window.txt`; e.g. runs 41739457344 and 41739472324 each took 2h20m, nine others were cancelled after
about an hour), plus its worker agents.

The spec's "13 minutes on a hosted runner" for the same candidate is not reproducible from the records: batch 2629 was
built once. Comparable evidence: the median Namespace 16x32 staging build is 9.8 min (p90 18.0); the single-PR batch
2628 immediately before it took 15 min on a fleet guest (run 41737087285, 4.7 min for the build step); the same 108
PRs failed earlier as batches 2615 (24 min, run 41725530762) and 2627 (46 min, run 41729728122), both on fleet
`0fb9966f`. The spec sentence should cite batch 2629 / run 41739726184 and drop the "same candidate" claim, or say
"a comparable candidate".

Other outliers (same source): run 41464145394, 200 min, success, fleet `0fb9966f` (2026-11-02); run 40952749583,
205 min, failure, GitHub-hosted after a 62 min wait (2026-10-29); GitHub-hosted waits up to 274 min
(run 40817396078, 2026-10-29).

## 2. Distribution over the last 14 days

Sources: TauCetiCI records (`records/runs/TauCeti/2026/10/21..28`, field `jobs[]` of each run record) for
Oct 21 to 27, and the Actions API (`/actions/workflows/pr-build.yml/runs?event=repository_dispatch` by day, then
`/runs/<id>/attempts/<n>/jobs`) for Oct 28 to Nov 4, because TauCetiCI's hourly collector last ran
2026-10-27T23:15Z and its records stop at 2026-10-28T01:00Z (cursor `state/cursor.json`; `db` release asset updated
2026-10-28T01:07Z). 999 `sandboxed-build` attempts, Bors `repository_dispatch` only. Minutes; wait = job created to
started; run = started to completed (cancelled excluded from run); build step = "Build exact candidate under bwrap".

| runner | n | success | wait p50 | wait p90 | wait max | run p50 | run p90 | run max | build step p50 | build step max |
|---|---|---|---|---|---|---|---|---|---|---|
| fleet `11e2756f` (tauchonki) | 484 | 342 | 0.0 | 0.0 | 0.2 | 12.9 | 23.4 | 166.0 | 6.7 | 94.8 |
| Namespace 16x32 (`nscloud-ubuntu-24.04-amd64-16x32-with-features;job.priority=1`) | 252 | 168 | 0.2 | 1.9 | 14.6 | 9.8 | 18.0 | 36.8 | 4.5 | 24.6 |
| fleet `4fab2fed` | 104 | 65 | 0.0 | 0.0 | 0.1 | 13.0 | 17.3 | 47.0 | 5.3 | 29.6 |
| GitHub-hosted `ubuntu-24.04` | 79 | 38 | 5.8 | 60.6 | 274.4 | 21.1 | 60.9 | 205.2 | 11.2 | 195.2 |
| fleet, legacy labels (`self-hosted,linux,x64,tauceti-fleet`, before Oct 23) | 73 | 47 | 0.0 | 0.0 | 0.0 | 12.6 | 15.9 | 44.0 | 7.2 | 38.4 |
| fleet `0fb9966f` | 4 | 1 | 0.0 | 0.0 | 0.0 | 66.4 | 200.0 | 200.0 | 53.5 | 186.9 |
| fleet `a26dbb02` | 3 | 1 | 0.0 | 0.0 | 0.0 | 31.2 | 31.2 | 31.2 | 24.1 | 24.1 |

Reading: fleet guests never queue but have a long tail (p90 23 min, max 166 min on tauchonki); Namespace 16x32 is
the fastest and tightest (p50 9.8, p90 18, max 37, wait p90 under 2 min); GitHub-hosted is slow both ways and only
appears when both other pools are saturated. Machine ids other than tauchonki are enrolled through the fleet broker
(`fleet.taucetiproject.org`, authenticated) and are not mapped to `chris`/`yael`/`brett` in any readable file.
Per-day pool mix is in `data/distribution.md`; since Oct 28 almost everything has gone to the fleet
(e.g. Nov 3: 55 fleet, 5 Namespace, 1 GitHub).

Sizing: Bors caps itself at 3 concurrent staging builds; the 7-day statistics feed shows 515 completed batches
(about 74 per day). A dedicated pool of 2 to 3 runners at the Namespace 16x32 shape covers that with the observed
p90 of 18 minutes.

## 3. What "dedicated staging runners, independent of the queue" requires

### (a) A Namespace runner profile reserved for staging

Visible configuration: Namespace is used through the `namespace-managed-runners` GitHub App; nothing in the repos
provisions it, and the workflows only reference the label grammar
(`nscloud-ubuntu-24.04-amd64-<cpu>x<mem>-with-features;job.priority=N` plus
`namespace-features:container.privileged=true;github.run-id=<id>`). Shapes in use: 2x4 and 4x8 (control and
publisher jobs, priority 0), 8x16 (PR overflow and maintenance, priority 1), 16x32 (staging and nightly, priority 1).
The 16x32 nightly comment calls it "the already provisioned sixteen-core Namespace build profile"
(`nightly-verify.yml:64-74`). `pr-build.yml:60-65` (Sep 2026) records that labels not provisioned for this
organisation leave the job queued "with an empty runner name until it expires"; the first Namespace move (#6644) hit
exactly that and was reverted (#6650).

Namespace documentation (fetched 2026-11-04): a Runner Profile has its own "Concurrent runners per profile" limit
under Advanced Settings, kept below the workspace concurrency limit so one profile cannot consume everything; jobs over
a profile's limit stay queued. `job.priority` only reorders pending jobs under the workspace limit, it reserves
nothing. Privileged containers on a profile are requested inline:
`namespace-profile-<tag>;container.privileged=true`. Profiles are created in the dashboard or with
`nsc github profile create --tag <tag> --os ubuntu-24.04 --machine_type 16x32`; the label shown on the profile page
is the one to use (docs show both `namespace-profile-<tag>` and `nscloud-<tag>` forms; the picker already treats both
prefixes as Namespace, `fleet_runner.py:244-245`).

Pros: isolated VM per job; placement and duration are already recorded (runner name `nsc-runner-*`, labels);
fastest and least variable pool in the data; no fleet host is withdrawn from worker or PR duty; the operator action
is one profile and two settings; rollback is clearing one variable. Cons: paid minutes (about 74 builds/day at
about 10 to 18 min on 16 vCPU); the profile's slots still count against the workspace concurrency limit, so the
workspace limit must have headroom above `NAMESPACE_BUILD_CAP=20` or the cap must be lowered; the picker's direct
scan counts `namespace-profile-*` jobs as Namespace build jobs (`fleet_runner.py:244-253`), reducing PR overflow
capacity by at most the profile limit.

### (b) A self-hosted runner on a dedicated fleet host with no worker agents

TauCetiFleet already supports a staging-only host: `tauceti-fleet enable HOST --ci staging --max-jobs 1
[--ci-vcpus 16 --ci-memory-gib 32]` registers JIT runners labelled `self-hosted, linux, x64, fleet-<machine_id>,
fleet-instance-<id>, tauceti-fleet, tauceti-fleet-staging` in runner group `TauCetiFleet`
(`/tmp/staging-runner/TauCetiFleet/tauceti_fleet/github.py:121-129`, `capacity.py:78-105`); not enabling
`--workers` keeps agents off it. TauCetiFleet PR #119 (merged 2026-11-04 13:57 AEDT, after the 2h46m build) adds
`reserve-staging HOST --slots N` and CPUWeight 10000 for staging guests on mixed hosts; its README calls this
"scheduling precedence, not a physical CPU reservation", so it mitigates but does not isolate.

Pros: no marginal cost; bigger guests possible (default 32 vCPU / 64 GiB); existing tooling and runner group policy.
Cons: it takes a whole host out of worker and PR duty (one of four); a job bound to exact fleet labels queues
indefinitely if that host is down, and bypassing the picker loses its two-minute unacquired-runner recovery
(TauCetiFleet README, "GitHub CI setup"); per-host variance is large in the data (fleet `0fb9966f`: 200 min build);
guest startup is rate limited to one per minute per host, so back-to-back candidates wait for a fresh guest; the
runner is only "dedicated" as long as nobody re-enables workers or `pr` on it.

### Minimal workflow change

`/tmp/staging-runner/pr-build-dedicated-runner.patch` (against `TauCetiProject/TauCeti` `main` at `6c781b0a73`,
branch `staging-dedicated-runner` in `/tmp/staging-runner/TauCeti`, not pushed; YAML parses; `actionlint` reports no
new findings beyond the pre-existing ones). It changes only `sandboxed-build.runs-on`:

```
runs-on: ${{ (github.event_name == 'repository_dispatch' && github.event.action == 'tauceti-bors-staging' && vars.STAGING_RUNS_ON != '')
             && fromJSON(vars.STAGING_RUNS_ON)
             || ( <existing expression, unchanged> ) }}
```

With repository variable `STAGING_RUNS_ON` unset the workflow behaves exactly as today for every event. When it holds
a JSON array of labels, only Bors staging dispatches use it; PR, maintenance, manual and `merge_group` builds keep the
picker. `pick-runner` still runs for staging (10 s on a 2x4 control runner) and its output is ignored; the
finalizer and publisher jobs stay on shared Namespace 2x4/4x8 at priority 0. The variable can carry either option:

- (a) `["namespace-profile-tauceti-staging;container.privileged=true;job.priority=0"]` (use the exact label the
  profile page shows); no `github.run-id` pin is needed because only staging jobs target the profile;
- (b) `["self-hosted","linux","x64","tauceti-fleet-staging","fleet-<machine_id>"]` for a staging-only fleet host.

## 4. Operator request

Recommended: option (a) first, because it is the only one that is isolated, recorded and reversible without touching
the fleet.

1. In the Namespace workspace that serves `TauCetiProject` (the one behind the `namespace-managed-runners` App),
   create a Runner Profile: tag `tauceti-staging`, OS `ubuntu-24.04`, machine type `16x32`, amd64; Advanced
   Settings "Concurrent runners per profile" = 3 (Bors's own cap). Confirm the workspace concurrency limit is at
   least `NAMESPACE_BUILD_CAP` (20) + 3, or lower `NAMESPACE_BUILD_CAP` to 17, so staging never queues behind PR
   overflow. Note the exact label shown on the profile page.
2. Prove the label is served before routing anything to it: dispatch `pr-build.yml` manually
   (`workflow_dispatch`, any open PR) from a scratch branch whose `runs-on` is the profile label, or ask Namespace
   support to confirm the profile is enabled for the organisation installation. The failure mode is a job that
   sits queued with an empty runner name.
3. Land the patch (no behaviour change on its own), then set repository variable
   `STAGING_RUNS_ON = ["namespace-profile-tauceti-staging;container.privileged=true;job.priority=0"]` on
   `TauCetiProject/TauCeti`. No Bors change is needed; dispatch, title matching and the external cap are unaffected.
4. Verify on the next Bors dispatch: `gh run list -R TauCetiProject/TauCeti -w pr-build.yml -e repository_dispatch
   -L 5`, then `gh api repos/TauCetiProject/TauCeti/actions/runs/<id>/jobs --jq '.jobs[] |
   select(.name=="sandboxed-build") | {runner_name, labels, created_at, started_at, completed_at}'`: `runner_name`
   should be `nsc-runner-*` and `labels` should contain the profile label. In TauCetiCI the same facts are
   `jobs[].runner_name`, `jobs[].labels` and `jobs[].runner_kind` (`github` / `namespace` / `self-hosted`) in each
   run record under `records/runs/TauCeti/<yyyy>/<mm>/<dd>/`, and columns `runner_kind`, `runner_name`, `labels`,
   `runner_size`, `wait_s`, `run_s` of table `jobs` in the `db` release. Both are stale: collection stopped on
   2026-10-28 and the successor `services/live-ci` (`ci-data.taucetiproject.org`) is documented as shadow mode, so
   restarting `collect.yml` or confirming live-ci is part of this request. Watch for a week that staging wait p90
   stays under 2 min and run p90 under 20 min (the current Namespace 16x32 figures), and that no `sandboxed-build`
   for a `bors branch=` run reports a `fleet-*` or `GitHub Actions *` runner.
5. Rollback: clear `STAGING_RUNS_ON`; staging returns to the picker on the next dispatch.

If (b) is preferred instead: pick one host, `tauceti-fleet drain HOST`, then `tauceti-fleet enable HOST --ci staging
--max-jobs 1` without `--workers`, confirm in `tauceti-fleet status HOST` that worker admissions are zero and a
`tauceti-fleet-staging` runner is online in runner group `TauCetiFleet`, set `STAGING_RUNS_ON` to the five fleet
labels above with that host's `machine_id`, and remove the host from `pr_order` in `tauceti.toml` so PR builds stop
being placed there. Verification is the same, with `runner_name` `fleet-<machine_id prefix>-*`.

## Side findings

- TauCetiCI's hourly collector has not run since 2026-10-27T23:15Z; records, the SQLite release and the published
  report all end on 2026-10-28. Any "last two weeks" analysis has to go to the Actions API for the second week.
- The Bors journal has `runner_assigned_at/started_at/finished_at` slots but they are null for every retained
  candidate; the spec's "controller records which runner and host executed each candidate build" is not yet true for
  Bors.
- TauCetiFleet #119 (staging CPUWeight 10000 and `reserve-staging`) landed today; the 2h46m build predates it. It
  will reduce but not remove contention on mixed hosts, and the data above gives the before measurement.

# GitHub zombie runs in TauCeti

GitHub has left several hundred TauCeti Actions runs reporting `status: queued` forever.
Such a run has no jobs, holds no runner, is referenced by no queue attempt and has no
effect on CI, Bors or the queue. These are known as zombie runs. They are harmless, and
GitHub refuses to cancel or delete them.

## Policy

Decided by Kim on 2026-11-20 and permanent:

- We never file GitHub support requests about zombie runs. That option is off the table.
- Known zombies are recorded here and excluded from all reporting: waiting counts,
  oldest-wait figures, status text and alerts. Otherwise they are ignored.
- This holds as long as they hold no runner and block nothing. A queued run that holds
  a runner or blocks a queue attempt is not a zombie; handle it as an ordinary fault.
- Do not report, investigate, cancel or delete them, and do not raise them with the
  operator. New ones that meet the rule below need no action either: the reporting
  ignores them automatically.

## Recognising one

A zombie run is queued, was created more than 24 hours ago and lists no jobs:

```sh
gh api repos/TauCetiProject/TauCeti/actions/runs/RUN_ID --jq '{status,conclusion,created_at,updated_at}'
gh api repos/TauCetiProject/TauCeti/actions/runs/RUN_ID/jobs --jq .total_count   # 0
```

A queued run that does have jobs is not a zombie: plain cancel clears it. For example,
https://github.com/TauCetiProject/TauCeti/actions/runs/44206125715 (a superseded
source-candidate run with a queued `finalize-merge-group-build` job) cancelled normally
on 2026-11-19.

The live-CI backlog summary (TauCetiCI `services/live-ci`) counts a job not running
24 hours after creation, a workflow queued with no jobs for 24 hours, and a workflow
whose remaining jobs are all such jobs, only in `stale_waiting_jobs` and the `stale`
counts. Its pool activity counts such jobs only in `stale_waiting`. `tauceti-fleet
status` leaves all of these out of its text; only `--json` carries the counts. Some of
those jobs are ones GitHub has since completed but whose completion the live-CI service
missed. Neither kind is live demand.

## What was tried

On 2026-11-19 (AEDT), for every remaining zombie that was tried:

- `gh run cancel RUN_ID` (`POST .../actions/runs/RUN_ID/cancel`): HTTP 409. For the
  2026-10-07 runs the message was "Cannot cancel a workflow run that is completed"; for
  the November runs it was "Cannot cancel a workflow run that has not been queued yet".
- `POST .../actions/runs/RUN_ID/force-cancel`: HTTP 409, "Cannot cancel a workflow run
  that has not been queued yet".
- `DELETE .../actions/runs/RUN_ID`: HTTP 204 for all 23 runs created on 2026-10-07,
  which then returned 404. HTTP 403 "Could not delete the workflow run" for every
  November run tried, repeatedly, with core quota to spare and with the same token that deleted the October runs.

We will not ask GitHub support to clear the remaining runs (see Policy). If GitHub ever
clears them on its own, update this record.

## Record

`github-zombie-runs.json` lists every run ID with its creation time, workflow, event
and result. None of them appeared in `ci_attempts.run_id`, `ci_request_runs.run_id` or
`ci_requests.owner_run_id` in the queue database.

| Created (UTC) | Runs | Result | Workflows |
|---|---|---|---|
| 2026-10-07 | 23 | deleted | Auto-merge, PR status, Merge sweep, pr-build, Rebuild on full-lint, Roadmap label |
| 2026-11-05 | 1 | remains | PR status |
| 2026-11-11 | 749 | remains | PR status, pr-build, No Stacked PRs, Roadmap label, Labels from comments, Rebuild on full-lint |
| 2026-11-16 | 2 | remains | pr-build (`workflow_dispatch`) |

Other repositories have the same kind of run, all created on 2026-11-11 with 0 jobs; cancel
answered HTTP 409 "Cannot cancel a workflow run that has not been queued yet" for all of
them on 2026-11-20:

- TauCetiProject/TauCetiWorker: 43098418872, 43097862918, 43097648530, 43096836233,
  43096415697, 43096219621.
- TauCetiProject/TauCetiFleet: 43098235388, 43097585081, 43097423738, 43097204029,
  43096639114.

The list endpoint (`GET .../actions/runs?status=queued`) shifts pages while new runs
arrive, so a single pass can miss or repeat a few runs. The record is the union of two
passes.

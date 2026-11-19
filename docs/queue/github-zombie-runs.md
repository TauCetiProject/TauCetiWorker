# GitHub zombie runs in TauCeti

GitHub has left several hundred TauCeti Actions runs reporting `status: queued` forever.
Such a run has no jobs, holds no runner, is referenced by no queue attempt and has no
effect on CI, Bors or the queue. It only inflates counts of queued work. These are
known as zombie runs. They are harmless, and the remaining ones cannot be cleared
without GitHub support. Do not report them, investigate them, or try to cancel or
delete them.

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

`tauceti-fleet status` lists jobs that have been queued for more than 24 hours on a
separate "Stale queued (known GitHub zombies, nothing to do)" line, outside the
Run/Wait column. Some of those are jobs GitHub has since completed but whose
completion the live-CI service missed. Neither kind is live demand.

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

A draft GitHub support request for the remaining runs is kept outside the repository.
If GitHub clears them, update this record.

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

The list endpoint (`GET .../actions/runs?status=queued`) shifts pages while new runs
arrive, so a single pass can miss or repeat a few runs. The record is the union of two
passes.

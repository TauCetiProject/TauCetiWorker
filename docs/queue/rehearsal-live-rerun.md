# Queue cutover rehearsal (live)

Repository `TauCetiProject/TauCetiQueueScratch`, service `http://127.0.0.1:8781`, started 2026-11-04T16:55:11Z, finished 2026-11-04T16:58:40Z.
Result: **PASS** (6 passed, 0 failed, 0 skipped).

| Scenario | Plan scenarios | Status | Seconds | Detail |
| --- | --- | --- | --- | --- |
| clean: Clean insertion, rebase, CI and publication with zero agent invocations | 1, 4, 5, 8 | pass | 152 |  |
| eligibility: Approval through the review App's `merge eligibility` check | 1 | pass | 0 |  |
| restart: A service restart mid-head recovers the running build without repeating work | 7, 12 | pass | 53 |  |
| chain: Every landed head is a single-parent chain in bijection with its members, with both trailers | 3 | pass | 0 |  |
| feeds: The observation and candidates feeds answer with valid schemas throughout the run | 22 | pass | 0 |  |
| status: Status names the running revision, and the runner and host of every candidate build | 22 | pass | 0 |  |

## Facts

```json
{
 "mode": "live",
 "repository": "TauCetiProject/TauCetiQueueScratch",
 "tag": "20261104T165511",
 "repository_id": 1445515892,
 "default_branch": "main",
 "private": false,
 "installation_repositories": [
  "TauCetiProject/TauCeti",
  "TauCetiProject/TauCetiQueueScratch"
 ],
 "app": {
  "id": 5607007,
  "slug": "tauceti-queue",
  "registered_permissions": {
   "actions": "write",
   "checks": "write",
   "contents": "write",
   "issues": "write",
   "metadata": "read",
   "pull_requests": "write",
   "statuses": "write",
   "workflows": "write"
  },
  "installation_permissions": {
   "actions": "write",
   "checks": "write",
   "contents": "write",
   "issues": "write",
   "metadata": "read",
   "pull_requests": "write",
   "statuses": "write",
   "workflows": "write"
  }
 },
 "operator": "kim-em",
 "operator_permission": "admin",
 "scaffold": {
  "pushed": false,
  "tip": "ccca8bccca918ea03f10b0aa4756f1a0a7c483f9"
 },
 "service_starts": [
  {
   "pid": 159012,
   "at": "2026-11-04T16:55:14Z"
  },
  {
   "pid": 169290,
   "at": "2026-11-04T16:58:16Z"
  }
 ],
 "status_errors": [
  {
   "at": "2026-11-04T16:55:14Z",
   "error": "GET /status answered 0: {'error': '<urlopen error [Errno 111] Connection refused>'}"
  },
  {
   "at": "2026-11-04T16:58:16Z",
   "error": "GET /status answered 0: {'error': '<urlopen error [Errno 111] Connection refused>'}"
  }
 ],
 "webhooks": "synthesized from the API objects the rehearsal created (no App deliveries were relayed within 30 s: GET /app/hook/deliveries answered 404)",
 "service_stops": [
  {
   "pid": 159012,
   "rc": 0,
   "at": "2026-11-04T16:58:16Z"
  },
  {
   "pid": 169290,
   "rc": 0,
   "at": "2026-11-04T16:58:41Z"
  }
 ],
 "relay": {
  "deliveries": 0,
  "events": {},
  "errors": []
 },
 "github_client": {
  "requests": 53,
  "retries": 0,
  "limit_waits": 0,
  "failures": 0,
  "rate_limit_remaining": 4510,
  "rate_limit": 6350,
  "rate_limit_reset_at": 1793811564.0,
  "last_error": null
 }
}
```

## clean: Clean insertion, rebase, CI and publication with zero agent invocations

Status: pass.

```json
{
 "prs": {
  "34": "fdf66ecef8c51b03df968ebbbdcfe0cac0d31862",
  "35": "e25b0fd450f77768171d6840c718d609a4962b9a"
 },
 "approval_a": {
  "via": "comment",
  "comment_id": 6636641662,
  "comment_url": "https://github.com/TauCetiProject/TauCetiQueueScratch/pull/34#issuecomment-6636641662",
  "actor": "kim-em"
 },
 "approval_b": {
  "via": "eligibility",
  "check_id": 126722942035,
  "app_id": 5607007,
  "merge_base_sha": "ccca8bccca918ea03f10b0aa4756f1a0a7c483f9",
  "html_url": "https://github.com/TauCetiProject/TauCetiQueueScratch/runs/126722942035"
 },
 "heads": [
  {
   "batch_id": 1,
   "version": 1,
   "base": "ccca8bccca918ea03f10b0aa4756f1a0a7c483f9",
   "tip": "81c9a83b6314f40c730203140a8e53c2aa1562e4",
   "members": [
    {
     "number": 34,
     "source_pr": "TauCetiProject/TauCetiQueueScratch#34",
     "accepted_head": "fdf66ecef8c51b03df968ebbbdcfe0cac0d31862",
     "commit": "81c9a83b6314f40c730203140a8e53c2aa1562e4",
     "provenance": "clean",
     "task_id": null
    }
   ],
   "ci": {
    "run_id": "41872485745",
    "html_url": "https://github.com/TauCetiProject/TauCetiQueueScratch/actions/runs/41872485745",
    "runner": "nsc-runner-0gj9m1o99eo2o",
    "host": "nsc-runner-0gj9m1o99eo2o",
    "conclusion": null,
    "outcome": "pending",
    "infrastructure_retry": 0,
    "pass_event": {
     "id": 21,
     "kind": "ci-pass",
     "source": "queue.controller",
     "observed_at": 1793811404132648,
     "details": {
      "attempt_id": 1,
      "run_id": "41872485745"
     }
    },
    "pass_run_id": "41872485745"
   },
   "counters": {
    "ci_dispatches": 2,
    "ci_passes": 1,
    "head_rebases": 1,
    "heads_landed": 1,
    "heads_sealed": 2,
    "prs_edited_landed": 0,
    "prs_landed": 1
   },
   "chain": {
    "ok": true,
    "commits": [
     {
      "source_pr": "TauCetiProject/TauCetiQueueScratch#34",
      "accepted_head": "fdf66ecef8c51b03df968ebbbdcfe0cac0d31862",
      "sha": "81c9a83b6314f40c730203140a8e53c2aa1562e4",
      "status": "clean",
      "empty": false,
      "conflict_paths": []
     }
    ],
    "members": [
     "TauCetiProject/TauCetiQueueScratch#34"
    ]
   }
  },
  {
   "batch_id": 2,
   "version": 2,
   "base": "81c9a83b6314f40c730203140a8e53c2aa1562e4",
   "tip": "18ccd6617eb8cefefa11e708b13f0dc58061a929",
   "members": [
    {
     "number": 35,
     "source_pr": "TauCetiProject/TauCetiQueueScratch#35",
     "accepted_head": "e25b0fd450f77768171d6840c718d609a4962b9a",
     "commit": "18ccd6617eb8cefefa11e708b13f0dc58061a929",
     "provenance": "clean",
     "task_id": null
    }
   ],
   "ci": {
    "run_id": "41872617342",
    "html_url": "https://github.com/TauCetiProject/TauCetiQueueScratch/actions/runs/41872617342",
    "runner": "nsc-runner-mv0v4ggnf9dqa",
    "host": "nsc-runner-mv0v4ggnf9dqa",
    "conclusion": null,
    "outcome": "pending",
    "infrastructure_retry": 0,
    "pass_event": {
     "id": 28,
     "kind": "ci-pass",
     "source": "queue.controller",
     "observed_at": 1793811459347921,
     "details": {
      "attempt_id": 2,
      "run_id": "41872617342"
     }
    },
    "pass_run_id": "41872617342"
   },
   "counters": {
    "ci_dispatches": 2,
    "ci_passes": 2,
    "head_rebases": 1,
    "heads_landed": 2,
    "heads_sealed": 2,
    "prs_edited_landed": 0,
    "prs_landed": 2
   },
   "chain": {
    "ok": true,
    "commits": [
     {
      "source_pr": "TauCetiProject/TauCetiQueueScratch#35",
      "accepted_head": "e25b0fd450f77768171d6840c718d609a4962b9a",
      "sha": "18ccd6617eb8cefefa11e708b13f0dc58061a929",
      "status": "clean",
      "empty": false,
      "conflict_paths": []
     }
    ],
    "members": [
     "TauCetiProject/TauCetiQueueScratch#35"
    ]
   }
  }
 ],
 "agent_invocations": 0,
 "pr_34": {
  "number": 34,
  "accepted_head": "fdf66ecef8c51b03df968ebbbdcfe0cac0d31862",
  "state": "closed",
  "title": "[Merged by Bors] - clean-a (20261104T165511)",
  "comment_id": 6636673823,
  "comment_url": "https://github.com/TauCetiProject/TauCetiQueueScratch/pull/34#issuecomment-6636673823",
  "comment_excerpt": "Pull request successfully merged into main."
 },
 "pr_35": {
  "number": 35,
  "accepted_head": "e25b0fd450f77768171d6840c718d609a4962b9a",
  "state": "closed",
  "title": "[Merged by Bors] - clean-b (20261104T165511)",
  "comment_id": 6636695085,
  "comment_url": "https://github.com/TauCetiProject/TauCetiQueueScratch/pull/35#issuecomment-6636695085",
  "comment_excerpt": "Pull request successfully merged into main."
 }
}
```

## eligibility: Approval through the review App's `merge eligibility` check

Status: pass.

```json
{
 "via": "eligibility",
 "check_id": 126722942035,
 "app_id": 5607007,
 "merge_base_sha": "ccca8bccca918ea03f10b0aa4756f1a0a7c483f9",
 "html_url": "https://github.com/TauCetiProject/TauCetiQueueScratch/runs/126722942035",
 "pr": 35,
 "published_as": "published under the queue's own App (installation permission checks: write)"
}
```

## restart: A service restart mid-head recovers the running build without repeating work

Status: pass.

```json
{
 "before": {
  "started_at": "2026-11-04T16:55:14Z",
  "revision": "e183ca051918ff623d02f2d1fa2d12c40c158688",
  "head": {
   "id": 4,
   "version": 1,
   "tip": "baba0e2201025926c314c290e8ea551ea2efe522"
  },
  "run_id": "41872800660",
  "ci_dispatches": 3
 },
 "after": {
  "started_at": "2026-11-04T16:58:16Z",
  "revision": "0393bc14aaa2b3f361e437e04e1c28dedce87e40",
  "head": {
   "id": 4,
   "version": 1,
   "tip": "baba0e2201025926c314c290e8ea551ea2efe522"
  },
  "run_id": "41872800660",
  "ci_dispatches": 3
 },
 "landed": {
  "run_id": "41872800660",
  "html_url": "https://github.com/TauCetiProject/TauCetiQueueScratch/actions/runs/41872800660",
  "runner": "nsc-runner-2683o997s82be",
  "host": "nsc-runner-2683o997s82be",
  "conclusion": null,
  "outcome": "pending",
  "infrastructure_retry": 0,
  "pass_event": {
   "id": 44,
   "kind": "ci-pass",
   "source": "queue.controller",
   "observed_at": 1793811513070084,
   "details": {
    "attempt_id": 3,
    "run_id": "41872800660"
   }
  },
  "pass_run_id": "41872800660"
 },
 "pr": {
  "number": 36,
  "accepted_head": "4eedbac774d35fb1538a1ebef9eac0bfb0596de9",
  "state": "closed",
  "title": "[Merged by Bors] - romeo (20261104T165511)",
  "comment_id": 6636715507,
  "comment_url": "https://github.com/TauCetiProject/TauCetiQueueScratch/pull/36#issuecomment-6636715507",
  "comment_excerpt": "Pull request successfully merged into main."
 }
}
```

## chain: Every landed head is a single-parent chain in bijection with its members, with both trailers

Status: pass.

```json
{
 "heads": [
  {
   "batch_id": 1,
   "version": 1,
   "base": "ccca8bccca918ea03f10b0aa4756f1a0a7c483f9",
   "tip": "81c9a83b6314f40c730203140a8e53c2aa1562e4",
   "commits": [
    {
     "source_pr": "TauCetiProject/TauCetiQueueScratch#34",
     "accepted_head": "fdf66ecef8c51b03df968ebbbdcfe0cac0d31862",
     "sha": "81c9a83b6314f40c730203140a8e53c2aa1562e4",
     "status": "clean",
     "empty": false,
     "conflict_paths": []
    }
   ],
   "members": [
    "TauCetiProject/TauCetiQueueScratch#34"
   ]
  },
  {
   "batch_id": 2,
   "version": 2,
   "base": "81c9a83b6314f40c730203140a8e53c2aa1562e4",
   "tip": "18ccd6617eb8cefefa11e708b13f0dc58061a929",
   "commits": [
    {
     "source_pr": "TauCetiProject/TauCetiQueueScratch#35",
     "accepted_head": "e25b0fd450f77768171d6840c718d609a4962b9a",
     "sha": "18ccd6617eb8cefefa11e708b13f0dc58061a929",
     "status": "clean",
     "empty": false,
     "conflict_paths": []
    }
   ],
   "members": [
    "TauCetiProject/TauCetiQueueScratch#35"
   ]
  },
  {
   "batch_id": 4,
   "version": 1,
   "base": "18ccd6617eb8cefefa11e708b13f0dc58061a929",
   "tip": "baba0e2201025926c314c290e8ea551ea2efe522",
   "commits": [
    {
     "source_pr": "TauCetiProject/TauCetiQueueScratch#36",
     "accepted_head": "4eedbac774d35fb1538a1ebef9eac0bfb0596de9",
     "sha": "baba0e2201025926c314c290e8ea551ea2efe522",
     "status": "clean",
     "empty": false,
     "conflict_paths": []
    }
   ],
   "members": [
    "TauCetiProject/TauCetiQueueScratch#36"
   ]
  }
 ]
}
```

## feeds: The observation and candidates feeds answer with valid schemas throughout the run

Status: pass.

```json
{
 "checks": [
  {
   "where": "before the first approval",
   "errors": [],
   "batch_ids": [],
   "batch_states": [],
   "candidates": 0,
   "promoted": 0
  },
  {
   "where": "during the CI run of head 1",
   "errors": [],
   "batch_ids": [
    2,
    1
   ],
   "batch_states": [
    "waiting",
    "running"
   ],
   "candidates": 1,
   "promoted": 0
  },
  {
   "where": "during the CI run of head 2",
   "errors": [],
   "batch_ids": [
    3,
    2
   ],
   "batch_states": [
    "waiting",
    "running"
   ],
   "candidates": 2,
   "promoted": 1
  },
  {
   "where": "during the CI run of head 4",
   "errors": [],
   "batch_ids": [
    5,
    4
   ],
   "batch_states": [
    "waiting",
    "running"
   ],
   "candidates": 3,
   "promoted": 2
  },
  {
   "where": "after the last scenario",
   "errors": [],
   "batch_ids": [],
   "batch_states": [],
   "candidates": 3,
   "promoted": 3
  }
 ]
}
```

## status: Status names the running revision, and the runner and host of every candidate build

Status: pass.

```json
{
 "revision": "0393bc14aaa2b3f361e437e04e1c28dedce87e40",
 "runs": [
  {
   "run_id": "41872485745",
   "html_url": "https://github.com/TauCetiProject/TauCetiQueueScratch/actions/runs/41872485745",
   "runner": "nsc-runner-0gj9m1o99eo2o",
   "host": "nsc-runner-0gj9m1o99eo2o",
   "conclusion": null,
   "outcome": "pending",
   "infrastructure_retry": 0,
   "pass_event": {
    "id": 21,
    "kind": "ci-pass",
    "source": "queue.controller",
    "observed_at": 1793811404132648,
    "details": {
     "attempt_id": 1,
     "run_id": "41872485745"
    }
   },
   "pass_run_id": "41872485745"
  },
  {
   "run_id": "41872617342",
   "html_url": "https://github.com/TauCetiProject/TauCetiQueueScratch/actions/runs/41872617342",
   "runner": "nsc-runner-mv0v4ggnf9dqa",
   "host": "nsc-runner-mv0v4ggnf9dqa",
   "conclusion": null,
   "outcome": "pending",
   "infrastructure_retry": 0,
   "pass_event": {
    "id": 28,
    "kind": "ci-pass",
    "source": "queue.controller",
    "observed_at": 1793811459347921,
    "details": {
     "attempt_id": 2,
     "run_id": "41872617342"
    }
   },
   "pass_run_id": "41872617342"
  },
  {
   "run_id": "41872800660",
   "html_url": "https://github.com/TauCetiProject/TauCetiQueueScratch/actions/runs/41872800660",
   "runner": "nsc-runner-2683o997s82be",
   "host": "nsc-runner-2683o997s82be",
   "conclusion": null,
   "outcome": "pending",
   "infrastructure_retry": 0,
   "pass_event": {
    "id": 44,
    "kind": "ci-pass",
    "source": "queue.controller",
    "observed_at": 1793811513070084,
    "details": {
     "attempt_id": 3,
     "run_id": "41872800660"
    }
   },
   "pass_run_id": "41872800660"
  }
 ],
 "schema": "tauceti-queue.status/v1"
}
```

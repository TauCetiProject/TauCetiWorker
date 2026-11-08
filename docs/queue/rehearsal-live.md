# Queue cutover rehearsal (live)

Repository `TauCetiProject/TauCetiQueueScratch`, service `http://127.0.0.1:8781`, started 2026-11-04T12:28:11Z, finished 2026-11-04T15:31:55Z.
Result: **FAIL** (12 passed, 1 failed, 1 skipped).

| Scenario | Plan scenarios | Status | Seconds | Detail |
| --- | --- | --- | --- | --- |
| clean: Clean insertion, rebase, CI and publication with zero agent invocations | 1, 4, 5, 8 | pass | 9292 |  |
| eligibility: Approval through the review App's `merge eligibility` check | 1 | skip | 0 | the installation's `checks` permission is 'read' (registration: 'write'); publishing a check run needs `checks: write` accepted on the installation; approval fell back to `bors r+` comments |
| source-failure: A source failure makes a repair task; the real worker path repairs it in place | 2, 4, 5, 11 | pass | 236 |  |
| shrink: A contribution the agent cannot make pass is shrunk to an empty commit; the comment names what was dropped | 3, 14 | pass | 220 |  |
| conflict: An admin advance rebases the running head; a conflicting member goes to a rebase-conflict task; a PR that did not replay cleanly is probed again and inserts without an agent | 2, 9, 10 | pass | 279 |  |
| urgent: An urgent PR is in the next head and lands by promotion without cancelling the running head | 18, 20 | pass | 133 |  |
| stale-status: A build status bound to a superseded run is not a verdict; the candidate is unchanged and re-dispatched | 10, 19 | pass | 292 |  |
| cancelled-run: A cancelled candidate run leaves the candidate unchanged and is re-dispatched | 19 | pass | 211 |  |
| closed-member: A member PR closed by automation after admission still lands; closure is not withdrawal | 16, 21 | pass | 0 |  |
| inference-hold: Workers reporting inference unavailable put the queue on hold with membership intact; the first successful task lifts it | 13 | pass | 266 |  |
| restart: A service restart mid-head recovers the running build without repeating work | 7, 12 | fail | 91 | POST /admin/settings answered 401: {'error': 'a GitHub token in `Authorization: Bearer` is required'} |
| chain: Every landed head is a single-parent chain in bijection with its members, with both trailers | 3 | pass | 0 |  |
| feeds: The observation and candidates feeds answer with valid schemas throughout the run | 22 | pass | 0 |  |
| status: Status names the running revision, and the runner and host of every candidate build | 22 | pass | 0 |  |

## Facts

```json
{
 "mode": "live",
 "repository": "TauCetiProject/TauCetiQueueScratch",
 "tag": "20261104T122811",
 "repository_id": 1445515892,
 "default_branch": "main",
 "private": true,
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
   "checks": "read",
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
  "tip": "3bcf579344de68b08218bff3e5ef46177d9dff83"
 },
 "service_starts": [
  {
   "pid": 1316671,
   "at": "2026-11-04T12:28:14Z"
  },
  {
   "pid": 3513182,
   "at": "2026-11-04T15:31:54Z"
  }
 ],
 "status_errors": [
  {
   "at": "2026-11-04T12:28:14Z",
   "error": "GET /status answered 0: {'error': '<urlopen error [Errno 111] Connection refused>'}"
  },
  {
   "at": "2026-11-04T13:38:49Z",
   "error": "GET /status answered 500: {'error': 'internal error'}"
  },
  {
   "at": "2026-11-04T15:31:54Z",
   "error": "GET /status answered 0: {'error': '<urlopen error [Errno 111] Connection refused>'}"
  }
 ],
 "webhooks": "synthesized from the API objects the rehearsal created (no App deliveries were relayed within 30 s: GET /app/hook/deliveries answered 404)",
 "service_stops": [
  {
   "pid": 1316671,
   "rc": 0,
   "at": "2026-11-04T15:31:54Z"
  },
  {
   "pid": 3513182,
   "rc": 0,
   "at": "2026-11-04T15:31:56Z"
  }
 ],
 "relay": {
  "deliveries": 0,
  "events": {},
  "errors": []
 },
 "github_client": {
  "requests": 1866,
  "retries": 0,
  "limit_waits": 0,
  "failures": 0,
  "rate_limit_remaining": 4176,
  "rate_limit": 5000,
  "rate_limit_reset_at": 1793807573.0,
  "last_error": null
 }
}
```

## clean: Clean insertion, rebase, CI and publication with zero agent invocations

Status: pass.

```json
{
 "prs": {
  "10": "723d0f556000492a3acca54572e44185657f73b6",
  "11": "6a6aca0aebef61506965be6db517aa6b3dd5be4a"
 },
 "approval_a": {
  "via": "comment",
  "comment_id": 6630600251,
  "comment_url": "https://github.com/TauCetiProject/TauCetiQueueScratch/pull/10#issuecomment-6630600251",
  "actor": "kim-em"
 },
 "approval_b": {
  "via": "comment",
  "comment_id": 6630611723,
  "comment_url": "https://github.com/TauCetiProject/TauCetiQueueScratch/pull/11#issuecomment-6630611723",
  "actor": "kim-em"
 },
 "heads": [
  {
   "batch_id": 1,
   "version": 3,
   "base": "397051b792b775ee80ee5ceddf8b6b3870311b1f",
   "tip": "1c2927dae7909f38d62da3732b37f7c3af25ae47",
   "members": [
    {
     "number": 10,
     "source_pr": "TauCetiProject/TauCetiQueueScratch#10",
     "accepted_head": "723d0f556000492a3acca54572e44185657f73b6",
     "commit": "1c2927dae7909f38d62da3732b37f7c3af25ae47",
     "provenance": "clean",
     "task_id": null
    }
   ],
   "ci": {
    "run_id": "41853323144",
    "html_url": "https://github.com/TauCetiProject/TauCetiQueueScratch/actions/runs/41853323144",
    "runner": "nsc-runner-0q9fsjh9ds0oq",
    "host": "nsc-runner-0q9fsjh9ds0oq",
    "conclusion": null,
    "outcome": "pending",
    "infrastructure_retry": 0,
    "pass_event": {
     "id": 34,
     "kind": "ci-pass",
     "source": "queue.controller",
     "observed_at": 1793804507659509,
     "details": {
      "attempt_id": 5,
      "run_id": "41853323144"
     }
    },
    "pass_run_id": "41853323144"
   },
   "counters": {
    "ci_dispatches": 6,
    "ci_passes": 1,
    "ci_redispatches": 2,
    "head_rebases": 3,
    "heads_landed": 1,
    "heads_sealed": 2,
    "prs_edited_landed": 0,
    "prs_landed": 1
   },
   "chain": {
    "ok": true,
    "commits": [
     {
      "source_pr": "TauCetiProject/TauCetiQueueScratch#10",
      "accepted_head": "723d0f556000492a3acca54572e44185657f73b6",
      "sha": "1c2927dae7909f38d62da3732b37f7c3af25ae47",
      "status": "clean",
      "empty": false,
      "conflict_paths": []
     }
    ],
    "members": [
     "TauCetiProject/TauCetiQueueScratch#10"
    ]
   }
  },
  {
   "batch_id": 2,
   "version": 2,
   "base": "1c2927dae7909f38d62da3732b37f7c3af25ae47",
   "tip": "1434aaa9a1cdc3cc64d96d70a0db20abc5c105bb",
   "members": [
    {
     "number": 11,
     "source_pr": "TauCetiProject/TauCetiQueueScratch#11",
     "accepted_head": "6a6aca0aebef61506965be6db517aa6b3dd5be4a",
     "commit": "1434aaa9a1cdc3cc64d96d70a0db20abc5c105bb",
     "provenance": "clean",
     "task_id": null
    }
   ],
   "ci": {
    "run_id": "41853525014",
    "html_url": "https://github.com/TauCetiProject/TauCetiQueueScratch/actions/runs/41853525014",
    "runner": "nsc-runner-53ohjtlu8ca1a",
    "host": "nsc-runner-53ohjtlu8ca1a",
    "conclusion": null,
    "outcome": "pending",
    "infrastructure_retry": 0,
    "pass_event": {
     "id": 41,
     "kind": "ci-pass",
     "source": "queue.controller",
     "observed_at": 1793804577603787,
     "details": {
      "attempt_id": 6,
      "run_id": "41853525014"
     }
    },
    "pass_run_id": "41853525014"
   },
   "counters": {
    "ci_dispatches": 6,
    "ci_passes": 2,
    "ci_redispatches": 2,
    "head_rebases": 3,
    "heads_landed": 2,
    "heads_sealed": 2,
    "prs_edited_landed": 0,
    "prs_landed": 2
   },
   "chain": {
    "ok": true,
    "commits": [
     {
      "source_pr": "TauCetiProject/TauCetiQueueScratch#11",
      "accepted_head": "6a6aca0aebef61506965be6db517aa6b3dd5be4a",
      "sha": "1434aaa9a1cdc3cc64d96d70a0db20abc5c105bb",
      "status": "clean",
      "empty": false,
      "conflict_paths": []
     }
    ],
    "members": [
     "TauCetiProject/TauCetiQueueScratch#11"
    ]
   }
  }
 ],
 "agent_invocations": 0,
 "pr_10": {
  "number": 10,
  "accepted_head": "723d0f556000492a3acca54572e44185657f73b6",
  "state": "closed",
  "title": "[Merged by Bors] - clean-a (20261104T122811)",
  "comment_id": 6634018217,
  "comment_url": "https://github.com/TauCetiProject/TauCetiQueueScratch/pull/10#issuecomment-6634018217",
  "comment_excerpt": "Pull request successfully merged into main."
 },
 "pr_11": {
  "number": 11,
  "accepted_head": "6a6aca0aebef61506965be6db517aa6b3dd5be4a",
  "state": "closed",
  "title": "[Merged by Bors] - clean-b (20261104T122811)",
  "comment_id": 6634045873,
  "comment_url": "https://github.com/TauCetiProject/TauCetiQueueScratch/pull/11#issuecomment-6634045873",
  "comment_excerpt": "Pull request successfully merged into main."
 }
}
```

## eligibility: Approval through the review App's `merge eligibility` check

Status: skip. the installation's `checks` permission is 'read' (registration: 'write'); publishing a check run needs `checks: write` accepted on the installation; approval fell back to `bors r+` comments


## source-failure: A source failure makes a repair task; the real worker path repairs it in place

Status: pass.

```json
{
 "prs": {
  "12": "0e00d36950519c03c09e176f43d88d45eca23abb",
  "13": "a5f05da71c91645fdb76f3108135b5a42bba8df5"
 },
 "failed_run": {
  "run_id": "41853969006",
  "html_url": "https://github.com/TauCetiProject/TauCetiQueueScratch/actions/runs/41853969006",
  "outcome": "source-failure",
  "outcome_detail": "Build failed: lake build --iofail reported errors"
 },
 "task": {
  "task_id": 1,
  "kind": "repair",
  "diagnostics": [
   {
    "column": 0,
    "kind": "compiler",
    "line_number": 5,
    "message": "error: TauCeti/R20261104T122811/Broken.lean:5:0: Not a definitional equality: the left-hand side",
    "module": "TauCeti.R20261104T122811.Broken",
    "path": "TauCeti/R20261104T122811/Broken.lean"
   },
   {
    "column": 30,
    "kind": "compiler",
    "line_number": 5,
    "message": "error: TauCeti/R20261104T122811/Broken.lean:5:30: Type mismatch",
    "module": "TauCeti.R20261104T122811.Broken",
    "path": "TauCeti/R20261104T122811/Broken.lean"
   }
  ]
 },
 "adoption": {
  "task_id": "1",
  "status": "adopted",
  "reason": null,
  "detail": null,
  "tip": "0ddde43c3853e703c62284c538f86cb893b1c385",
  "version": 2,
  "pushed_ref": "refs/tauceti-queue/submissions/1/8",
  "worker": "rehearsal@chungus2",
  "seconds": 4.7,
  "lease": {
   "renewals": 0,
   "errors": 0,
   "lost": false
  }
 },
 "heads": [
  {
   "batch_id": 4,
   "version": 2,
   "base": "1434aaa9a1cdc3cc64d96d70a0db20abc5c105bb",
   "tip": "0ddde43c3853e703c62284c538f86cb893b1c385",
   "members": [
    {
     "number": 12,
     "source_pr": "TauCetiProject/TauCetiQueueScratch#12",
     "accepted_head": "0e00d36950519c03c09e176f43d88d45eca23abb",
     "commit": "0ddde43c3853e703c62284c538f86cb893b1c385",
     "provenance": "edited",
     "task_id": 1
    }
   ],
   "ci": {
    "run_id": "41854083037",
    "html_url": "https://github.com/TauCetiProject/TauCetiQueueScratch/actions/runs/41854083037",
    "runner": "nsc-runner-ooj77o1fcnqms",
    "host": "nsc-runner-ooj77o1fcnqms",
    "conclusion": null,
    "outcome": "pending",
    "infrastructure_retry": 0,
    "pass_event": {
     "id": 62,
     "kind": "ci-pass",
     "source": "queue.controller",
     "observed_at": 1793804760736702,
     "details": {
      "attempt_id": 9,
      "run_id": "41854083037"
     }
    },
    "pass_run_id": "41854083037"
   },
   "counters": {
    "ci_dispatches": 10,
    "ci_passes": 3,
    "ci_redispatches": 3,
    "ci_source_failures": 1,
    "head_rebases": 4,
    "heads_landed": 3,
    "heads_sealed": 4,
    "prs_edited_landed": 1,
    "prs_landed": 3,
    "repair_rounds": 1,
    "tasks_repair": 1
   },
   "chain": {
    "ok": true,
    "commits": [
     {
      "source_pr": "TauCetiProject/TauCetiQueueScratch#12",
      "accepted_head": "0e00d36950519c03c09e176f43d88d45eca23abb",
      "sha": "0ddde43c3853e703c62284c538f86cb893b1c385",
      "status": "clean",
      "empty": false,
      "conflict_paths": []
     }
    ],
    "members": [
     "TauCetiProject/TauCetiQueueScratch#12"
    ]
   }
  },
  {
   "batch_id": 5,
   "version": 2,
   "base": "0ddde43c3853e703c62284c538f86cb893b1c385",
   "tip": "e75532665ed889f5d4e26ec23925e37db95739f1",
   "members": [
    {
     "number": 13,
     "source_pr": "TauCetiProject/TauCetiQueueScratch#13",
     "accepted_head": "a5f05da71c91645fdb76f3108135b5a42bba8df5",
     "commit": "e75532665ed889f5d4e26ec23925e37db95739f1",
     "provenance": "clean",
     "task_id": null
    }
   ],
   "ci": {
    "run_id": "41854241774",
    "html_url": "https://github.com/TauCetiProject/TauCetiQueueScratch/actions/runs/41854241774",
    "runner": "nsc-runner-kicie2ld26cei",
    "host": "nsc-runner-kicie2ld26cei",
    "conclusion": null,
    "outcome": "pending",
    "infrastructure_retry": 0,
    "pass_event": {
     "id": 69,
     "kind": "ci-pass",
     "source": "queue.controller",
     "observed_at": 1793804815688108,
     "details": {
      "attempt_id": 10,
      "run_id": "41854241774"
     }
    },
    "pass_run_id": "41854241774"
   },
   "counters": {
    "ci_dispatches": 10,
    "ci_passes": 4,
    "ci_redispatches": 3,
    "ci_source_failures": 1,
    "head_rebases": 4,
    "heads_landed": 4,
    "heads_sealed": 4,
    "prs_edited_landed": 1,
    "prs_landed": 4,
    "repair_rounds": 1,
    "tasks_repair": 1
   },
   "chain": {
    "ok": true,
    "commits": [
     {
      "source_pr": "TauCetiProject/TauCetiQueueScratch#13",
      "accepted_head": "a5f05da71c91645fdb76f3108135b5a42bba8df5",
      "sha": "e75532665ed889f5d4e26ec23925e37db95739f1",
      "status": "clean",
      "empty": false,
      "conflict_paths": []
     }
    ],
    "members": [
     "TauCetiProject/TauCetiQueueScratch#13"
    ]
   }
  }
 ],
 "pr_broken": {
  "number": 12,
  "accepted_head": "0e00d36950519c03c09e176f43d88d45eca23abb",
  "state": "closed",
  "title": "[Merged by Bors] - broken (20261104T122811)",
  "comment_id": 6634118201,
  "comment_url": "https://github.com/TauCetiProject/TauCetiQueueScratch/pull/12#issuecomment-6634118201",
  "comment_excerpt": "Pull request successfully merged into main."
 },
 "pr_beside": {
  "number": 13,
  "accepted_head": "a5f05da71c91645fdb76f3108135b5a42bba8df5",
  "state": "closed",
  "title": "[Merged by Bors] - beside (20261104T122811)",
  "comment_id": 6634139446,
  "comment_url": "https://github.com/TauCetiProject/TauCetiQueueScratch/pull/13#issuecomment-6634139446",
  "comment_excerpt": "Pull request successfully merged into main."
 },
 "dispatches_for_head": {
  "run_id": "41854083037",
  "html_url": "https://github.com/TauCetiProject/TauCetiQueueScratch/actions/runs/41854083037",
  "runner": "nsc-runner-ooj77o1fcnqms",
  "host": "nsc-runner-ooj77o1fcnqms",
  "conclusion": null,
  "outcome": "pending",
  "infrastructure_retry": 0,
  "pass_event": {
   "id": 62,
   "kind": "ci-pass",
   "source": "queue.controller",
   "observed_at": 1793804760736702,
   "details": {
    "attempt_id": 9,
    "run_id": "41854083037"
   }
  },
  "pass_run_id": "41854083037"
 }
}
```

## shrink: A contribution the agent cannot make pass is shrunk to an empty commit; the comment names what was dropped

Status: pass.

```json
{
 "prs": {
  "14": "c9930d1202c442986471fe0d5de2758371cc8e5e",
  "15": "6993cc04c6c12f3fdc6b0a373b37b644ff93b253"
 },
 "task": {
  "task_id": 2,
  "diagnostics": [
   {
    "column": 0,
    "kind": "compiler",
    "line_number": 5,
    "message": "error: TauCeti/R20261104T122811/Hopeless.lean:5:0: Not a definitional equality: the left-hand side",
    "module": "TauCeti.R20261104T122811.Hopeless",
    "path": "TauCeti/R20261104T122811/Hopeless.lean"
   },
   {
    "column": 28,
    "kind": "compiler",
    "line_number": 5,
    "message": "error: TauCeti/R20261104T122811/Hopeless.lean:5:28: Type mismatch",
    "module": "TauCeti.R20261104T122811.Hopeless",
    "path": "TauCeti/R20261104T122811/Hopeless.lean"
   }
  ]
 },
 "adoption": {
  "task_id": "2",
  "status": "adopted",
  "reason": null,
  "detail": null,
  "tip": "b15e9f401c371f9ebaf7a2b661837c1c3dc5fd08",
  "version": 2,
  "pushed_ref": "refs/tauceti-queue/submissions/2/12",
  "worker": "rehearsal@chungus2",
  "seconds": 3.3,
  "lease": {
   "renewals": 0,
   "errors": 0,
   "lost": false
  }
 },
 "empty_commit": {
  "source_pr": "TauCetiProject/TauCetiQueueScratch#14",
  "accepted_head": "c9930d1202c442986471fe0d5de2758371cc8e5e",
  "sha": "b15e9f401c371f9ebaf7a2b661837c1c3dc5fd08",
  "status": "clean",
  "empty": true,
  "conflict_paths": []
 },
 "pr_hopeless": {
  "number": 14,
  "accepted_head": "c9930d1202c442986471fe0d5de2758371cc8e5e",
  "state": "closed",
  "title": "[Merged by Bors] - hopeless (20261104T122811)",
  "comment_id": 6634204435,
  "comment_url": "https://github.com/TauCetiProject/TauCetiQueueScratch/pull/14#issuecomment-6634204435",
  "comment_excerpt": "Pull request successfully merged into main."
 },
 "pr_sound": {
  "number": 15,
  "accepted_head": "6993cc04c6c12f3fdc6b0a373b37b644ff93b253",
  "state": "closed",
  "title": "[Merged by Bors] - sound (20261104T122811)",
  "comment_id": 6634226020,
  "comment_url": "https://github.com/TauCetiProject/TauCetiQueueScratch/pull/15#issuecomment-6634226020",
  "comment_excerpt": "Pull request successfully merged into main."
 },
 "counters": {
  "shrink_to_empty": 1,
  "repair_rounds": 2
 }
}
```

## conflict: An admin advance rebases the running head; a conflicting member goes to a rebase-conflict task; a PR that did not replay cleanly is probed again and inserts without an agent

Status: pass.

```json
{
 "head_gamma": {
  "id": 10,
  "version": 1,
  "base": "50c765f3762496d6741436b68a7b1bf6c6eb6930",
  "tip": "9cb74121e8845c1d31defe5eb5e62901276490ec",
  "run_id": "41855056782"
 },
 "open_batch": {
  "id": 11,
  "base": "50c765f3762496d6741436b68a7b1bf6c6eb6930"
 },
 "admin_advance": "9c552aa3d33cdcd03bdd6f9f5d3a99d1e92d7d1d",
 "head_gamma_rebased": {
  "version": 2,
  "base": "9c552aa3d33cdcd03bdd6f9f5d3a99d1e92d7d1d",
  "tip": "4b8a031353df570ed1583c87161afdbad313bcf2",
  "run_id": "41855170574"
 },
 "xray_first_probe": {
  "number": 18,
  "head_sha": "59d2cc236b37b7cf23e814ec2b4c803c4554cde4",
  "approved_at": 1793805114.6980956,
  "priority": 0,
  "urgent": false,
  "passes_waited": 0,
  "probes": 1,
  "last_probe": "[\"TauCeti/Shared.lean\"]"
 },
 "landed_gamma": {
  "version": 2,
  "base": "9c552aa3d33cdcd03bdd6f9f5d3a99d1e92d7d1d",
  "tip": "4b8a031353df570ed1583c87161afdbad313bcf2",
  "ci": {
   "run_id": "41855170574",
   "html_url": "https://github.com/TauCetiProject/TauCetiQueueScratch/actions/runs/41855170574",
   "runner": "nsc-runner-hasighk3o6mmo",
   "host": "nsc-runner-hasighk3o6mmo",
   "conclusion": null,
   "outcome": "pending",
   "infrastructure_retry": 0,
   "pass_event": {
    "id": 116,
    "kind": "ci-pass",
    "source": "queue.controller",
    "observed_at": 1793805147650451,
    "details": {
     "attempt_id": 15,
     "run_id": "41855170574"
    }
   },
   "pass_run_id": "41855170574"
  }
 },
 "task": {
  "task_id": 3,
  "kind": "rebase_conflict",
  "diagnostics": [
   {
    "clean_prefix_tip": "4b8a031353df570ed1583c87161afdbad313bcf2",
    "kind": "conflict",
    "message": "replaying the chain onto 4b8a031353df conflicts at TauCetiProject/TauCetiQueueScratch#17",
    "onto": "4b8a031353df570ed1583c87161afdbad313bcf2",
    "paths": [
     "TauCeti/Shared.lean"
    ],
    "pending": [],
    "source_pr": "TauCetiProject/TauCetiQueueScratch#17"
   }
  ]
 },
 "adoption": {
  "task_id": "3",
  "status": "adopted",
  "reason": null,
  "detail": null,
  "tip": "a3516cfc03837cb3f1cb5d34b839d9b3351745fd",
  "version": 2,
  "pushed_ref": "refs/tauceti-queue/submissions/3/19",
  "worker": "rehearsal@chungus2",
  "seconds": 4.0,
  "lease": {
   "renewals": 0,
   "errors": 0,
   "lost": false
  }
 },
 "landed_yankee": {
  "version": 2,
  "base": "4b8a031353df570ed1583c87161afdbad313bcf2",
  "tip": "a3516cfc03837cb3f1cb5d34b839d9b3351745fd",
  "ci": {
   "run_id": "41855360908",
   "html_url": "https://github.com/TauCetiProject/TauCetiQueueScratch/actions/runs/41855360908",
   "runner": "nsc-runner-97eaavv7f3kq8",
   "host": "nsc-runner-97eaavv7f3kq8",
   "conclusion": null,
   "outcome": "pending",
   "infrastructure_retry": 0,
   "pass_event": {
    "id": 127,
    "kind": "ci-pass",
    "source": "queue.controller",
    "observed_at": 1793805211807687,
    "details": {
     "attempt_id": 16,
     "run_id": "41855360908"
    }
   },
   "pass_run_id": "41855360908"
  }
 },
 "probe_conflict_event": {
  "id": 115,
  "kind": "probe-conflict",
  "source": "queue.controller",
  "observed_at": 1793805115469190,
  "details": {
   "against_main": false,
   "number": 18,
   "paths": [
    "TauCeti/Shared.lean"
   ]
  }
 },
 "xray_inserted_event": {
  "id": 125,
  "kind": "inserted",
  "source": "queue.controller",
  "observed_at": 1793805158530785,
  "details": {
   "commit": "d29c24099af2b890bfa32100c6e34f7c1b554905",
   "empty": false,
   "number": 18,
   "version": 1
  }
 },
 "landed_xray": {
  "version": 2,
  "base": "a3516cfc03837cb3f1cb5d34b839d9b3351745fd",
  "tip": "d70f628d36290acbb2d1fb447ab48b6531c008d9",
  "ci": {
   "run_id": "41855523289",
   "html_url": "https://github.com/TauCetiProject/TauCetiQueueScratch/actions/runs/41855523289",
   "runner": "nsc-runner-jg1j8jcnhpnng",
   "host": "nsc-runner-jg1j8jcnhpnng",
   "conclusion": null,
   "outcome": "pending",
   "infrastructure_retry": 0,
   "pass_event": {
    "id": 134,
    "kind": "ci-pass",
    "source": "queue.controller",
    "observed_at": 1793805313623513,
    "details": {
     "attempt_id": 17,
     "run_id": "41855523289"
    }
   },
   "pass_run_id": "41855523289"
  }
 },
 "agent_invocations": 1,
 "pr_yankee": {
  "number": 17,
  "accepted_head": "5b5508a376189292ab26c8050ef56778d470ca06",
  "state": "closed",
  "title": "[Merged by Bors] - shared-y (20261104T122811)",
  "comment_id": 6634296605,
  "comment_url": "https://github.com/TauCetiProject/TauCetiQueueScratch/pull/17#issuecomment-6634296605",
  "comment_excerpt": "Pull request successfully merged into main."
 },
 "pr_xray": {
  "number": 18,
  "accepted_head": "59d2cc236b37b7cf23e814ec2b4c803c4554cde4",
  "state": "closed",
  "title": "[Merged by Bors] - shared-x (20261104T122811)",
  "comment_id": 6634337184,
  "comment_url": "https://github.com/TauCetiProject/TauCetiQueueScratch/pull/18#issuecomment-6634337184",
  "comment_excerpt": "Pull request successfully merged into main."
 },
 "pr_gamma": {
  "number": 16,
  "accepted_head": "00054721d3732d5a3b35b83c5045a202b418a522",
  "state": "closed",
  "title": "[Merged by Bors] - gamma (20261104T122811)",
  "comment_id": 6634271214,
  "comment_url": "https://github.com/TauCetiProject/TauCetiQueueScratch/pull/16#issuecomment-6634271214",
  "comment_excerpt": "Pull request successfully merged into main."
 }
}
```

## urgent: An urgent PR is in the next head and lands by promotion without cancelling the running head

Status: pass.

```json
{
 "running_head": {
  "id": 14,
  "tip": "3e68afde6e6e1356d53244b461246611001e3131",
  "run_id": "41855856868"
 },
 "mark_urgent": {
  "urgent": true,
  "state": "inserted"
 },
 "open_batch": {
  "id": 15,
  "members": [
   20,
   21,
   22,
   23
  ],
  "urgent": true
 },
 "landed_running_head": {
  "run_id": "41855856868",
  "html_url": "https://github.com/TauCetiProject/TauCetiQueueScratch/actions/runs/41855856868",
  "runner": "nsc-runner-8fqb7mu3pjdli",
  "host": "nsc-runner-8fqb7mu3pjdli",
  "conclusion": null,
  "outcome": "pending",
  "infrastructure_retry": 0,
  "pass_event": {
   "id": 159,
   "kind": "ci-pass",
   "source": "queue.controller",
   "observed_at": 1793805401249188,
   "details": {
    "attempt_id": 18,
    "run_id": "41855856868"
   }
  },
  "pass_run_id": "41855856868"
 },
 "landed_urgent_head": {
  "batch_id": 15,
  "version": 5,
  "tip": "64d6eb14943612e139b91285f754d05358df6739",
  "ci": {
   "run_id": "41856072985",
   "html_url": "https://github.com/TauCetiProject/TauCetiQueueScratch/actions/runs/41856072985",
   "runner": "nsc-runner-pclhq9jdvk59o",
   "host": "nsc-runner-pclhq9jdvk59o",
   "conclusion": null,
   "outcome": "pending",
   "infrastructure_retry": 0,
   "pass_event": {
    "id": 166,
    "kind": "ci-pass",
    "source": "queue.controller",
    "observed_at": 1793805440297264,
    "details": {
     "attempt_id": 19,
     "run_id": "41856072985"
    }
   },
   "pass_run_id": "41856072985"
  },
  "members": [
   {
    "number": 20,
    "source_pr": "TauCetiProject/TauCetiQueueScratch#20",
    "accepted_head": "fb95d88500aa8d5136b29abaa86abc44dbcc8d2e",
    "commit": "450a6b3adc07fc72c927106e22009a8f11850eb4",
    "provenance": "clean",
    "task_id": null
   },
   {
    "number": 21,
    "source_pr": "TauCetiProject/TauCetiQueueScratch#21",
    "accepted_head": "478c9b0446b13ca54ee79d9a702410146bcf697e",
    "commit": "47ad2893f1469e31dfbbd5b92f20d220d3ffde36",
    "provenance": "clean",
    "task_id": null
   },
   {
    "number": 22,
    "source_pr": "TauCetiProject/TauCetiQueueScratch#22",
    "accepted_head": "241497c1bfa8f636123425c49ed199ebf77cc0b9",
    "commit": "4e6b7fddfc5a3a6fd84d19fd2ad839ad20b13006",
    "provenance": "clean",
    "task_id": null
   },
   {
    "number": 23,
    "source_pr": "TauCetiProject/TauCetiQueueScratch#23",
    "accepted_head": "1444307c8ed216487d9fcc7a3c695ea2b87cacd9",
    "commit": "64d6eb14943612e139b91285f754d05358df6739",
    "provenance": "clean",
    "task_id": null
   }
  ]
 },
 "promotion_event": {
  "id": 168,
  "kind": "landed",
  "source": "queue.controller",
  "observed_at": 1793805441566188,
  "details": {
   "how": "promoted",
   "members": [
    20,
    21,
    22,
    23
   ],
   "tip": "64d6eb14943612e139b91285f754d05358df6739"
  }
 },
 "pr_urgent": {
  "number": 23,
  "accepted_head": "1444307c8ed216487d9fcc7a3c695ea2b87cacd9",
  "state": "closed",
  "title": "[Merged by Bors] - urgent (20261104T122811)",
  "comment_id": 6634391289,
  "comment_url": "https://github.com/TauCetiProject/TauCetiQueueScratch/pull/23#issuecomment-6634391289",
  "comment_excerpt": "Pull request successfully merged into main."
 }
}
```

## stale-status: A build status bound to a superseded run is not a verdict; the candidate is unchanged and re-dispatched

Status: pass.

```json
{
 "first_attempt": {
  "run_id": "41856244431",
  "tip": "70f0786fbf4e813a8d63a16a8d509bfe9195a7bb",
  "version": 1
 },
 "planted": {
  "sha": "70f0786fbf4e813a8d63a16a8d509bfe9195a7bb",
  "run_id": "41853323144",
  "target_url": "https://github.com/TauCetiProject/TauCetiQueueScratch/actions/runs/41853323144"
 },
 "refusal": {
  "run_id": "41856244431",
  "outcome": "infrastructure",
  "outcome_detail": "missing-status: the completed run posted no bound build status",
  "conclusion": "success"
 },
 "journal": {
  "id": 179,
  "kind": "ci-infrastructure",
  "source": "queue.controller",
  "observed_at": 1793805660667433,
  "details": {
   "attempt_id": 20,
   "detail": "missing-status: the completed run posted no bound build status",
   "status_run": "41853323144"
  }
 },
 "second_attempt": {
  "run_id": "41856853811",
  "retry": 1,
  "tip": "70f0786fbf4e813a8d63a16a8d509bfe9195a7bb"
 },
 "landed": {
  "run_id": "41856853811",
  "html_url": "https://github.com/TauCetiProject/TauCetiQueueScratch/actions/runs/41856853811",
  "runner": "nsc-runner-6icoohlkkep4i",
  "host": "nsc-runner-6icoohlkkep4i",
  "conclusion": null,
  "outcome": "pending",
  "infrastructure_retry": 1,
  "pass_event": {
   "id": 181,
   "kind": "ci-pass",
   "source": "queue.controller",
   "observed_at": 1793805740125863,
   "details": {
    "attempt_id": 21,
    "run_id": "41856853811"
   }
  },
  "pass_run_id": "41856853811"
 },
 "pr": {
  "number": 24,
  "accepted_head": "be1f06b9e6d0e7d8cdcc59c709650fcb387f9743",
  "state": "closed",
  "title": "[Merged by Bors] - stale (20261104T122811)",
  "comment_id": 6634511519,
  "comment_url": "https://github.com/TauCetiProject/TauCetiQueueScratch/pull/24#issuecomment-6634511519",
  "comment_excerpt": "Pull request successfully merged into main."
 },
 "cleanup": "252f258f6b02ea7ceb1624d50e1e13d4c8a4f223"
}
```

## cancelled-run: A cancelled candidate run leaves the candidate unchanged and is re-dispatched

Status: pass.

```json
{
 "first_attempt": {
  "run_id": "41857110553",
  "tip": "0b75ce7c0349950d0a4f3fef17a30e072f7018e8"
 },
 "cancelled": {
  "run_id": "41857110553",
  "outcome": "infrastructure",
  "outcome_detail": "run-cancelled",
  "conclusion": "cancelled"
 },
 "second_attempt": {
  "run_id": "41857359630",
  "retry": 1
 },
 "landed": {
  "run_id": "41857359630",
  "html_url": "https://github.com/TauCetiProject/TauCetiQueueScratch/actions/runs/41857359630",
  "runner": "nsc-runner-kbsrdvdml5v9c",
  "host": "nsc-runner-kbsrdvdml5v9c",
  "conclusion": null,
  "outcome": "pending",
  "infrastructure_retry": 1,
  "pass_event": {
   "id": 201,
   "kind": "ci-pass",
   "source": "queue.controller",
   "observed_at": 1793805896207646,
   "details": {
    "attempt_id": 23,
    "run_id": "41857359630"
   }
  },
  "pass_run_id": "41857359630"
 },
 "pr_quebec": {
  "number": 25,
  "accepted_head": "126998e1159eade38e0cc0c51dead25bec14c4e2",
  "state": "closed",
  "title": "[Merged by Bors] - quebec (20261104T122811)",
  "comment_id": 6634574450,
  "comment_url": "https://github.com/TauCetiProject/TauCetiQueueScratch/pull/25#issuecomment-6634574450",
  "comment_excerpt": "Pull request successfully merged into main."
 }
}
```

## closed-member: A member PR closed by automation after admission still lands; closure is not withdrawal

Status: pass.

```json
{
 "closed": {
  "closed_by": "tauceti-queue[bot] (an automation)",
  "state": "closed"
 },
 "pr": 26,
 "member_of": 20,
 "landed_head": 20,
 "pr_record": {
  "number": 26,
  "accepted_head": "db510305500fd646274bb69b4fa4da9d70648ff0",
  "state": "closed",
  "title": "closed-member (20261104T122811)",
  "comment_id": 6634596710,
  "comment_url": "https://github.com/TauCetiProject/TauCetiQueueScratch/pull/26#issuecomment-6634596710",
  "comment_excerpt": "Pull request successfully merged into main."
 },
 "member_event_ignored": [
  {
   "id": 197,
   "kind": "member-event-ignored",
   "source": "queue.controller",
   "observed_at": 1793805785839346,
   "details": {
    "action": "closed",
    "number": 26
   }
  }
 ]
}
```

## inference-hold: Workers reporting inference unavailable put the queue on hold with membership intact; the first successful task lifts it

Status: pass.

```json
{
 "releases": [
  {
   "task_id": "4",
   "status": "released",
   "reason": "inference_unavailable",
   "detail": "provider returned 529",
   "tip": null,
   "version": null,
   "pushed_ref": null,
   "worker": "rehearsal@chungus2",
   "seconds": 2.1,
   "lease": {
    "renewals": 0,
    "errors": 0,
    "lost": false
   }
  },
  {
   "task_id": "4",
   "status": "released",
   "reason": "inference_unavailable",
   "detail": "provider returned 529",
   "tip": null,
   "version": null,
   "pushed_ref": null,
   "worker": "rehearsal@chungus2",
   "seconds": 1.9,
   "lease": {
    "renewals": 0,
    "errors": 0,
    "lost": false
   }
  },
  {
   "task_id": "4",
   "status": "released",
   "reason": "inference_unavailable",
   "detail": "provider returned 529",
   "tip": null,
   "version": null,
   "pushed_ref": null,
   "worker": "rehearsal@chungus2",
   "seconds": 0.8,
   "lease": {
    "renewals": 0,
    "errors": 0,
    "lost": false
   }
  }
 ],
 "hold": {
  "attempts": 0,
  "last_error": "provider returned 529",
  "last_report_at": 1793806039.2977903,
  "next_retry_at": 1793806099.2977903,
  "reporter": "rehearsal@chungus2",
  "reports": 3,
  "since": 1793806039.2977903
 },
 "inserted_during_hold": {
  "pr": 29,
  "batch": 23
 },
 "adoption": {
  "task_id": "4",
  "status": "adopted",
  "reason": null,
  "detail": null,
  "tip": "485ad828e2f6eadccb73303b8ec094e5e7cafc69",
  "version": 2,
  "pushed_ref": "refs/tauceti-queue/submissions/4/38",
  "worker": "rehearsal@chungus2",
  "seconds": 4.2,
  "lease": {
   "renewals": 0,
   "errors": 0,
   "lost": false
  }
 },
 "counters": {
  "inference_holds": 1,
  "repair_rounds": 3
 },
 "heads": [
  {
   "batch_id": 22,
   "tip": "485ad828e2f6eadccb73303b8ec094e5e7cafc69",
   "ci": {
    "run_id": "41857998028",
    "html_url": "https://github.com/TauCetiProject/TauCetiQueueScratch/actions/runs/41857998028",
    "runner": "nsc-runner-jjcvrgmihfl9g",
    "host": "nsc-runner-jjcvrgmihfl9g",
    "conclusion": null,
    "outcome": "pending",
    "infrastructure_retry": 0,
    "pass_event": {
     "id": 238,
     "kind": "ci-pass",
     "source": "queue.controller",
     "observed_at": 1793806145719675,
     "details": {
      "attempt_id": 26,
      "run_id": "41857998028"
     }
    },
    "pass_run_id": "41857998028"
   }
  },
  {
   "batch_id": 23,
   "tip": "e7e2d83e03f3078b668deb1559548d2a74647766",
   "ci": {
    "run_id": "41858241684",
    "html_url": "https://github.com/TauCetiProject/TauCetiQueueScratch/actions/runs/41858241684",
    "runner": "nsc-runner-165btueq3lduc",
    "host": "nsc-runner-165btueq3lduc",
    "conclusion": null,
    "outcome": "pending",
    "infrastructure_retry": 0,
    "pass_event": {
     "id": 245,
     "kind": "ci-pass",
     "source": "queue.controller",
     "observed_at": 1793806215127181,
     "details": {
      "attempt_id": 27,
      "run_id": "41858241684"
     }
    },
    "pass_run_id": "41858241684"
   }
  }
 ],
 "pr_victor": {
  "number": 27,
  "accepted_head": "812bb2b7f74dec1c4335d71a407e4e763bbc2dd5",
  "state": "closed",
  "title": "[Merged by Bors] - victor (20261104T122811)",
  "comment_id": 6634673806,
  "comment_url": "https://github.com/TauCetiProject/TauCetiQueueScratch/pull/27#issuecomment-6634673806",
  "comment_excerpt": "Pull request successfully merged into main."
 }
}
```

## restart: A service restart mid-head recovers the running build without repeating work

Status: fail. POST /admin/settings answered 401: {'error': 'a GitHub token in `Authorization: Bearer` is required'}

```json
{
 "before": {
  "started_at": "2026-11-04T12:28:14Z",
  "revision": "adfe1d7fa564d0c0eb712950324272ee2e7c99f6",
  "head": {
   "id": 25,
   "version": 1,
   "tip": "28b7f697b52c0ac46e59523ff30c1e81338f6360"
  },
  "run_id": "41858476009",
  "ci_dispatches": 28
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
   "version": 3,
   "base": "397051b792b775ee80ee5ceddf8b6b3870311b1f",
   "tip": "1c2927dae7909f38d62da3732b37f7c3af25ae47",
   "commits": [
    {
     "source_pr": "TauCetiProject/TauCetiQueueScratch#10",
     "accepted_head": "723d0f556000492a3acca54572e44185657f73b6",
     "sha": "1c2927dae7909f38d62da3732b37f7c3af25ae47",
     "status": "clean",
     "empty": false,
     "conflict_paths": []
    }
   ],
   "members": [
    "TauCetiProject/TauCetiQueueScratch#10"
   ]
  },
  {
   "batch_id": 2,
   "version": 2,
   "base": "1c2927dae7909f38d62da3732b37f7c3af25ae47",
   "tip": "1434aaa9a1cdc3cc64d96d70a0db20abc5c105bb",
   "commits": [
    {
     "source_pr": "TauCetiProject/TauCetiQueueScratch#11",
     "accepted_head": "6a6aca0aebef61506965be6db517aa6b3dd5be4a",
     "sha": "1434aaa9a1cdc3cc64d96d70a0db20abc5c105bb",
     "status": "clean",
     "empty": false,
     "conflict_paths": []
    }
   ],
   "members": [
    "TauCetiProject/TauCetiQueueScratch#11"
   ]
  },
  {
   "batch_id": 4,
   "version": 2,
   "base": "1434aaa9a1cdc3cc64d96d70a0db20abc5c105bb",
   "tip": "0ddde43c3853e703c62284c538f86cb893b1c385",
   "commits": [
    {
     "source_pr": "TauCetiProject/TauCetiQueueScratch#12",
     "accepted_head": "0e00d36950519c03c09e176f43d88d45eca23abb",
     "sha": "0ddde43c3853e703c62284c538f86cb893b1c385",
     "status": "clean",
     "empty": false,
     "conflict_paths": []
    }
   ],
   "members": [
    "TauCetiProject/TauCetiQueueScratch#12"
   ]
  },
  {
   "batch_id": 5,
   "version": 2,
   "base": "0ddde43c3853e703c62284c538f86cb893b1c385",
   "tip": "e75532665ed889f5d4e26ec23925e37db95739f1",
   "commits": [
    {
     "source_pr": "TauCetiProject/TauCetiQueueScratch#13",
     "accepted_head": "a5f05da71c91645fdb76f3108135b5a42bba8df5",
     "sha": "e75532665ed889f5d4e26ec23925e37db95739f1",
     "status": "clean",
     "empty": false,
     "conflict_paths": []
    }
   ],
   "members": [
    "TauCetiProject/TauCetiQueueScratch#13"
   ]
  },
  {
   "batch_id": 7,
   "version": 2,
   "base": "e75532665ed889f5d4e26ec23925e37db95739f1",
   "tip": "b15e9f401c371f9ebaf7a2b661837c1c3dc5fd08",
   "commits": [
    {
     "source_pr": "TauCetiProject/TauCetiQueueScratch#14",
     "accepted_head": "c9930d1202c442986471fe0d5de2758371cc8e5e",
     "sha": "b15e9f401c371f9ebaf7a2b661837c1c3dc5fd08",
     "status": "clean",
     "empty": true,
     "conflict_paths": []
    }
   ],
   "members": [
    "TauCetiProject/TauCetiQueueScratch#14"
   ]
  },
  {
   "batch_id": 8,
   "version": 2,
   "base": "b15e9f401c371f9ebaf7a2b661837c1c3dc5fd08",
   "tip": "50c765f3762496d6741436b68a7b1bf6c6eb6930",
   "commits": [
    {
     "source_pr": "TauCetiProject/TauCetiQueueScratch#15",
     "accepted_head": "6993cc04c6c12f3fdc6b0a373b37b644ff93b253",
     "sha": "50c765f3762496d6741436b68a7b1bf6c6eb6930",
     "status": "clean",
     "empty": false,
     "conflict_paths": []
    }
   ],
   "members": [
    "TauCetiProject/TauCetiQueueScratch#15"
   ]
  },
  {
   "batch_id": 10,
   "version": 2,
   "base": "9c552aa3d33cdcd03bdd6f9f5d3a99d1e92d7d1d",
   "tip": "4b8a031353df570ed1583c87161afdbad313bcf2",
   "commits": [
    {
     "source_pr": "TauCetiProject/TauCetiQueueScratch#16",
     "accepted_head": "00054721d3732d5a3b35b83c5045a202b418a522",
     "sha": "4b8a031353df570ed1583c87161afdbad313bcf2",
     "status": "clean",
     "empty": false,
     "conflict_paths": []
    }
   ],
   "members": [
    "TauCetiProject/TauCetiQueueScratch#16"
   ]
  },
  {
   "batch_id": 11,
   "version": 2,
   "base": "4b8a031353df570ed1583c87161afdbad313bcf2",
   "tip": "a3516cfc03837cb3f1cb5d34b839d9b3351745fd",
   "commits": [
    {
     "source_pr": "TauCetiProject/TauCetiQueueScratch#17",
     "accepted_head": "5b5508a376189292ab26c8050ef56778d470ca06",
     "sha": "a3516cfc03837cb3f1cb5d34b839d9b3351745fd",
     "status": "clean",
     "empty": false,
     "conflict_paths": []
    }
   ],
   "members": [
    "TauCetiProject/TauCetiQueueScratch#17"
   ]
  },
  {
   "batch_id": 12,
   "version": 2,
   "base": "a3516cfc03837cb3f1cb5d34b839d9b3351745fd",
   "tip": "d70f628d36290acbb2d1fb447ab48b6531c008d9",
   "commits": [
    {
     "source_pr": "TauCetiProject/TauCetiQueueScratch#18",
     "accepted_head": "59d2cc236b37b7cf23e814ec2b4c803c4554cde4",
     "sha": "d70f628d36290acbb2d1fb447ab48b6531c008d9",
     "status": "clean",
     "empty": false,
     "conflict_paths": []
    }
   ],
   "members": [
    "TauCetiProject/TauCetiQueueScratch#18"
   ]
  },
  {
   "batch_id": 14,
   "version": 1,
   "base": "d70f628d36290acbb2d1fb447ab48b6531c008d9",
   "tip": "3e68afde6e6e1356d53244b461246611001e3131",
   "commits": [
    {
     "source_pr": "TauCetiProject/TauCetiQueueScratch#19",
     "accepted_head": "10308e7be95953abd7e7dfe972e19c6f59a4cf8d",
     "sha": "3e68afde6e6e1356d53244b461246611001e3131",
     "status": "clean",
     "empty": false,
     "conflict_paths": []
    }
   ],
   "members": [
    "TauCetiProject/TauCetiQueueScratch#19"
   ]
  },
  {
   "batch_id": 15,
   "version": 5,
   "base": "3e68afde6e6e1356d53244b461246611001e3131",
   "tip": "64d6eb14943612e139b91285f754d05358df6739",
   "commits": [
    {
     "source_pr": "TauCetiProject/TauCetiQueueScratch#20",
     "accepted_head": "fb95d88500aa8d5136b29abaa86abc44dbcc8d2e",
     "sha": "450a6b3adc07fc72c927106e22009a8f11850eb4",
     "status": "clean",
     "empty": false,
     "conflict_paths": []
    },
    {
     "source_pr": "TauCetiProject/TauCetiQueueScratch#21",
     "accepted_head": "478c9b0446b13ca54ee79d9a702410146bcf697e",
     "sha": "47ad2893f1469e31dfbbd5b92f20d220d3ffde36",
     "status": "clean",
     "empty": false,
     "conflict_paths": []
    },
    {
     "source_pr": "TauCetiProject/TauCetiQueueScratch#22",
     "accepted_head": "241497c1bfa8f636123425c49ed199ebf77cc0b9",
     "sha": "4e6b7fddfc5a3a6fd84d19fd2ad839ad20b13006",
     "status": "clean",
     "empty": false,
     "conflict_paths": []
    },
    {
     "source_pr": "TauCetiProject/TauCetiQueueScratch#23",
     "accepted_head": "1444307c8ed216487d9fcc7a3c695ea2b87cacd9",
     "sha": "64d6eb14943612e139b91285f754d05358df6739",
     "status": "clean",
     "empty": false,
     "conflict_paths": []
    }
   ],
   "members": [
    "TauCetiProject/TauCetiQueueScratch#20",
    "TauCetiProject/TauCetiQueueScratch#21",
    "TauCetiProject/TauCetiQueueScratch#22",
    "TauCetiProject/TauCetiQueueScratch#23"
   ]
  },
  {
   "batch_id": 17,
   "version": 1,
   "base": "64d6eb14943612e139b91285f754d05358df6739",
   "tip": "70f0786fbf4e813a8d63a16a8d509bfe9195a7bb",
   "commits": [
    {
     "source_pr": "TauCetiProject/TauCetiQueueScratch#24",
     "accepted_head": "be1f06b9e6d0e7d8cdcc59c709650fcb387f9743",
     "sha": "70f0786fbf4e813a8d63a16a8d509bfe9195a7bb",
     "status": "clean",
     "empty": false,
     "conflict_paths": []
    }
   ],
   "members": [
    "TauCetiProject/TauCetiQueueScratch#24"
   ]
  },
  {
   "batch_id": 19,
   "version": 1,
   "base": "252f258f6b02ea7ceb1624d50e1e13d4c8a4f223",
   "tip": "0b75ce7c0349950d0a4f3fef17a30e072f7018e8",
   "commits": [
    {
     "source_pr": "TauCetiProject/TauCetiQueueScratch#25",
     "accepted_head": "126998e1159eade38e0cc0c51dead25bec14c4e2",
     "sha": "0b75ce7c0349950d0a4f3fef17a30e072f7018e8",
     "status": "clean",
     "empty": false,
     "conflict_paths": []
    }
   ],
   "members": [
    "TauCetiProject/TauCetiQueueScratch#25"
   ]
  },
  {
   "batch_id": 20,
   "version": 2,
   "base": "0b75ce7c0349950d0a4f3fef17a30e072f7018e8",
   "tip": "4bead677711cb1617defcedf60fb6e2fc62894de",
   "commits": [
    {
     "source_pr": "TauCetiProject/TauCetiQueueScratch#26",
     "accepted_head": "db510305500fd646274bb69b4fa4da9d70648ff0",
     "sha": "4bead677711cb1617defcedf60fb6e2fc62894de",
     "status": "clean",
     "empty": false,
     "conflict_paths": []
    }
   ],
   "members": [
    "TauCetiProject/TauCetiQueueScratch#26"
   ]
  },
  {
   "batch_id": 22,
   "version": 2,
   "base": "4bead677711cb1617defcedf60fb6e2fc62894de",
   "tip": "485ad828e2f6eadccb73303b8ec094e5e7cafc69",
   "commits": [
    {
     "source_pr": "TauCetiProject/TauCetiQueueScratch#27",
     "accepted_head": "812bb2b7f74dec1c4335d71a407e4e763bbc2dd5",
     "sha": "485ad828e2f6eadccb73303b8ec094e5e7cafc69",
     "status": "clean",
     "empty": false,
     "conflict_paths": []
    }
   ],
   "members": [
    "TauCetiProject/TauCetiQueueScratch#27"
   ]
  },
  {
   "batch_id": 23,
   "version": 3,
   "base": "485ad828e2f6eadccb73303b8ec094e5e7cafc69",
   "tip": "e7e2d83e03f3078b668deb1559548d2a74647766",
   "commits": [
    {
     "source_pr": "TauCetiProject/TauCetiQueueScratch#28",
     "accepted_head": "5838624020fae92fa568b86b6cdc47bcf76313a3",
     "sha": "af7ef2bb32b6697a71c09666805873eece240b78",
     "status": "clean",
     "empty": false,
     "conflict_paths": []
    },
    {
     "source_pr": "TauCetiProject/TauCetiQueueScratch#29",
     "accepted_head": "3c9109f09235dd819e73a47e66d4ce4cf71478a4",
     "sha": "e7e2d83e03f3078b668deb1559548d2a74647766",
     "status": "clean",
     "empty": false,
     "conflict_paths": []
    }
   ],
   "members": [
    "TauCetiProject/TauCetiQueueScratch#28",
    "TauCetiProject/TauCetiQueueScratch#29"
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
   "candidates": 6,
   "promoted": 5
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
   "candidates": 9,
   "promoted": 6
  },
  {
   "where": "during the CI run of head 5",
   "errors": [],
   "batch_ids": [
    6,
    5
   ],
   "batch_states": [
    "waiting",
    "running"
   ],
   "candidates": 10,
   "promoted": 9
  },
  {
   "where": "during the CI run of head 7",
   "errors": [],
   "batch_ids": [
    8,
    7
   ],
   "batch_states": [
    "waiting",
    "running"
   ],
   "candidates": 12,
   "promoted": 10
  },
  {
   "where": "during the CI run of head 8",
   "errors": [],
   "batch_ids": [
    9,
    8
   ],
   "batch_states": [
    "waiting",
    "running"
   ],
   "candidates": 13,
   "promoted": 12
  },
  {
   "where": "during the CI run of head 10",
   "errors": [],
   "batch_ids": [
    11,
    10
   ],
   "batch_states": [
    "waiting",
    "running"
   ],
   "candidates": 15,
   "promoted": 13
  },
  {
   "where": "during the CI run of head 11",
   "errors": [],
   "batch_ids": [
    12,
    11
   ],
   "batch_states": [
    "waiting",
    "running"
   ],
   "candidates": 16,
   "promoted": 15
  },
  {
   "where": "during the CI run of head 12",
   "errors": [],
   "batch_ids": [
    13,
    12
   ],
   "batch_states": [
    "waiting",
    "running"
   ],
   "candidates": 17,
   "promoted": 16
  },
  {
   "where": "during the CI run of head 14",
   "errors": [],
   "batch_ids": [
    15,
    14
   ],
   "batch_states": [
    "waiting",
    "running"
   ],
   "candidates": 18,
   "promoted": 17
  },
  {
   "where": "during the CI run of head 15",
   "errors": [],
   "batch_ids": [
    16,
    15
   ],
   "batch_states": [
    "waiting",
    "running"
   ],
   "candidates": 19,
   "promoted": 18
  },
  {
   "where": "during the CI run of head 17",
   "errors": [],
   "batch_ids": [
    18,
    17
   ],
   "batch_states": [
    "waiting",
    "running"
   ],
   "candidates": 21,
   "promoted": 19
  },
  {
   "where": "during the CI run of head 19",
   "errors": [],
   "batch_ids": [
    20,
    19
   ],
   "batch_states": [
    "waiting",
    "running"
   ],
   "candidates": 23,
   "promoted": 21
  },
  {
   "where": "during the CI run of head 20",
   "errors": [],
   "batch_ids": [
    21,
    20
   ],
   "batch_states": [
    "waiting",
    "running"
   ],
   "candidates": 24,
   "promoted": 23
  },
  {
   "where": "during the CI run of head 22",
   "errors": [],
   "batch_ids": [
    23,
    22
   ],
   "batch_states": [
    "waiting",
    "running"
   ],
   "candidates": 26,
   "promoted": 24
  },
  {
   "where": "during the CI run of head 23",
   "errors": [],
   "batch_ids": [
    24,
    23
   ],
   "batch_states": [
    "waiting",
    "running"
   ],
   "candidates": 27,
   "promoted": 26
  },
  {
   "where": "after the last scenario",
   "errors": [],
   "batch_ids": [
    26,
    25
   ],
   "batch_states": [
    "waiting",
    "running"
   ],
   "candidates": 28,
   "promoted": 27
  }
 ]
}
```

## status: Status names the running revision, and the runner and host of every candidate build

Status: pass.

```json
{
 "revision": "e82e0d85b1904695e3b5fffa8b712a7b093f080e",
 "runs": [
  {
   "run_id": "41853323144",
   "html_url": "https://github.com/TauCetiProject/TauCetiQueueScratch/actions/runs/41853323144",
   "runner": "nsc-runner-0q9fsjh9ds0oq",
   "host": "nsc-runner-0q9fsjh9ds0oq",
   "conclusion": null,
   "outcome": "pending",
   "infrastructure_retry": 0,
   "pass_event": {
    "id": 34,
    "kind": "ci-pass",
    "source": "queue.controller",
    "observed_at": 1793804507659509,
    "details": {
     "attempt_id": 5,
     "run_id": "41853323144"
    }
   },
   "pass_run_id": "41853323144"
  },
  {
   "run_id": "41853525014",
   "html_url": "https://github.com/TauCetiProject/TauCetiQueueScratch/actions/runs/41853525014",
   "runner": "nsc-runner-53ohjtlu8ca1a",
   "host": "nsc-runner-53ohjtlu8ca1a",
   "conclusion": null,
   "outcome": "pending",
   "infrastructure_retry": 0,
   "pass_event": {
    "id": 41,
    "kind": "ci-pass",
    "source": "queue.controller",
    "observed_at": 1793804577603787,
    "details": {
     "attempt_id": 6,
     "run_id": "41853525014"
    }
   },
   "pass_run_id": "41853525014"
  },
  {
   "run_id": "41854083037",
   "html_url": "https://github.com/TauCetiProject/TauCetiQueueScratch/actions/runs/41854083037",
   "runner": "nsc-runner-ooj77o1fcnqms",
   "host": "nsc-runner-ooj77o1fcnqms",
   "conclusion": null,
   "outcome": "pending",
   "infrastructure_retry": 0,
   "pass_event": {
    "id": 62,
    "kind": "ci-pass",
    "source": "queue.controller",
    "observed_at": 1793804760736702,
    "details": {
     "attempt_id": 9,
     "run_id": "41854083037"
    }
   },
   "pass_run_id": "41854083037"
  },
  {
   "run_id": "41854241774",
   "html_url": "https://github.com/TauCetiProject/TauCetiQueueScratch/actions/runs/41854241774",
   "runner": "nsc-runner-kicie2ld26cei",
   "host": "nsc-runner-kicie2ld26cei",
   "conclusion": null,
   "outcome": "pending",
   "infrastructure_retry": 0,
   "pass_event": {
    "id": 69,
    "kind": "ci-pass",
    "source": "queue.controller",
    "observed_at": 1793804815688108,
    "details": {
     "attempt_id": 10,
     "run_id": "41854241774"
    }
   },
   "pass_run_id": "41854241774"
  },
  {
   "run_id": "41854618677",
   "html_url": "https://github.com/TauCetiProject/TauCetiQueueScratch/actions/runs/41854618677",
   "runner": "nsc-runner-5713kkha1sgl2",
   "host": "nsc-runner-5713kkha1sgl2",
   "conclusion": null,
   "outcome": "pending",
   "infrastructure_retry": 0,
   "pass_event": {
    "id": 88,
    "kind": "ci-pass",
    "source": "queue.controller",
    "observed_at": 1793804981737535,
    "details": {
     "attempt_id": 12,
     "run_id": "41854618677"
    }
   },
   "pass_run_id": "41854618677"
  },
  {
   "run_id": "41854862200",
   "html_url": "https://github.com/TauCetiProject/TauCetiQueueScratch/actions/runs/41854862200",
   "runner": "nsc-runner-p9gf9ci2op582",
   "host": "nsc-runner-p9gf9ci2op582",
   "conclusion": null,
   "outcome": "pending",
   "infrastructure_retry": 0,
   "pass_event": {
    "id": 95,
    "kind": "ci-pass",
    "source": "queue.controller",
    "observed_at": 1793805035749427,
    "details": {
     "attempt_id": 13,
     "run_id": "41854862200"
    }
   },
   "pass_run_id": "41854862200"
  },
  {
   "run_id": "41855170574",
   "html_url": "https://github.com/TauCetiProject/TauCetiQueueScratch/actions/runs/41855170574",
   "runner": "nsc-runner-hasighk3o6mmo",
   "host": "nsc-runner-hasighk3o6mmo",
   "conclusion": null,
   "outcome": "pending",
   "infrastructure_retry": 0,
   "pass_event": {
    "id": 116,
    "kind": "ci-pass",
    "source": "queue.controller",
    "observed_at": 1793805147650451,
    "details": {
     "attempt_id": 15,
     "run_id": "41855170574"
    }
   },
   "pass_run_id": "41855170574"
  },
  {
   "run_id": "41855360908",
   "html_url": "https://github.com/TauCetiProject/TauCetiQueueScratch/actions/runs/41855360908",
   "runner": "nsc-runner-97eaavv7f3kq8",
   "host": "nsc-runner-97eaavv7f3kq8",
   "conclusion": null,
   "outcome": "pending",
   "infrastructure_retry": 0,
   "pass_event": {
    "id": 127,
    "kind": "ci-pass",
    "source": "queue.controller",
    "observed_at": 1793805211807687,
    "details": {
     "attempt_id": 16,
     "run_id": "41855360908"
    }
   },
   "pass_run_id": "41855360908"
  },
  {
   "run_id": "41855523289",
   "html_url": "https://github.com/TauCetiProject/TauCetiQueueScratch/actions/runs/41855523289",
   "runner": "nsc-runner-jg1j8jcnhpnng",
   "host": "nsc-runner-jg1j8jcnhpnng",
   "conclusion": null,
   "outcome": "pending",
   "infrastructure_retry": 0,
   "pass_event": {
    "id": 134,
    "kind": "ci-pass",
    "source": "queue.controller",
    "observed_at": 1793805313623513,
    "details": {
     "attempt_id": 17,
     "run_id": "41855523289"
    }
   },
   "pass_run_id": "41855523289"
  },
  {
   "run_id": "41855856868",
   "html_url": "https://github.com/TauCetiProject/TauCetiQueueScratch/actions/runs/41855856868",
   "runner": "nsc-runner-8fqb7mu3pjdli",
   "host": "nsc-runner-8fqb7mu3pjdli",
   "conclusion": null,
   "outcome": "pending",
   "infrastructure_retry": 0,
   "pass_event": {
    "id": 159,
    "kind": "ci-pass",
    "source": "queue.controller",
    "observed_at": 1793805401249188,
    "details": {
     "attempt_id": 18,
     "run_id": "41855856868"
    }
   },
   "pass_run_id": "41855856868"
  },
  {
   "run_id": "41856072985",
   "html_url": "https://github.com/TauCetiProject/TauCetiQueueScratch/actions/runs/41856072985",
   "runner": "nsc-runner-pclhq9jdvk59o",
   "host": "nsc-runner-pclhq9jdvk59o",
   "conclusion": null,
   "outcome": "pending",
   "infrastructure_retry": 0,
   "pass_event": {
    "id": 166,
    "kind": "ci-pass",
    "source": "queue.controller",
    "observed_at": 1793805440297264,
    "details": {
     "attempt_id": 19,
     "run_id": "41856072985"
    }
   },
   "pass_run_id": "41856072985"
  },
  {
   "run_id": "41856853811",
   "html_url": "https://github.com/TauCetiProject/TauCetiQueueScratch/actions/runs/41856853811",
   "runner": "nsc-runner-6icoohlkkep4i",
   "host": "nsc-runner-6icoohlkkep4i",
   "conclusion": null,
   "outcome": "pending",
   "infrastructure_retry": 1,
   "pass_event": {
    "id": 181,
    "kind": "ci-pass",
    "source": "queue.controller",
    "observed_at": 1793805740125863,
    "details": {
     "attempt_id": 21,
     "run_id": "41856853811"
    }
   },
   "pass_run_id": "41856853811"
  },
  {
   "run_id": "41857359630",
   "html_url": "https://github.com/TauCetiProject/TauCetiQueueScratch/actions/runs/41857359630",
   "runner": "nsc-runner-kbsrdvdml5v9c",
   "host": "nsc-runner-kbsrdvdml5v9c",
   "conclusion": null,
   "outcome": "pending",
   "infrastructure_retry": 1,
   "pass_event": {
    "id": 201,
    "kind": "ci-pass",
    "source": "queue.controller",
    "observed_at": 1793805896207646,
    "details": {
     "attempt_id": 23,
     "run_id": "41857359630"
    }
   },
   "pass_run_id": "41857359630"
  },
  {
   "run_id": "41857524367",
   "html_url": "https://github.com/TauCetiProject/TauCetiQueueScratch/actions/runs/41857524367",
   "runner": "nsc-runner-i4n4ofb396r8q",
   "host": "nsc-runner-i4n4ofb396r8q",
   "conclusion": null,
   "outcome": "pending",
   "infrastructure_retry": 0,
   "pass_event": {
    "id": 208,
    "kind": "ci-pass",
    "source": "queue.controller",
    "observed_at": 1793805950054018,
    "details": {
     "attempt_id": 24,
     "run_id": "41857524367"
    }
   },
   "pass_run_id": "41857524367"
  },
  {
   "run_id": "41857998028",
   "html_url": "https://github.com/TauCetiProject/TauCetiQueueScratch/actions/runs/41857998028",
   "runner": "nsc-runner-jjcvrgmihfl9g",
   "host": "nsc-runner-jjcvrgmihfl9g",
   "conclusion": null,
   "outcome": "pending",
   "infrastructure_retry": 0,
   "pass_event": {
    "id": 238,
    "kind": "ci-pass",
    "source": "queue.controller",
    "observed_at": 1793806145719675,
    "details": {
     "attempt_id": 26,
     "run_id": "41857998028"
    }
   },
   "pass_run_id": "41857998028"
  },
  {
   "run_id": "41858241684",
   "html_url": "https://github.com/TauCetiProject/TauCetiQueueScratch/actions/runs/41858241684",
   "runner": "nsc-runner-165btueq3lduc",
   "host": "nsc-runner-165btueq3lduc",
   "conclusion": null,
   "outcome": "pending",
   "infrastructure_retry": 0,
   "pass_event": {
    "id": 245,
    "kind": "ci-pass",
    "source": "queue.controller",
    "observed_at": 1793806215127181,
    "details": {
     "attempt_id": 27,
     "run_id": "41858241684"
    }
   },
   "pass_run_id": "41858241684"
  }
 ],
 "schema": "tauceti-queue.status/v1"
}
```

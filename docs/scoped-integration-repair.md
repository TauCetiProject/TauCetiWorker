# One named integration repair: inactive proposal

This option is **off by default**. Code preparation or merge does not authorize a foreign-fork task. Explicit human authorization belongs in the original #16813 window. Peer coordination is not approval. All nine science workers stay disabled.

The exception admits only one named PR/exact head in `tauceti-ai-for-science/TauCeti` to the existing rebase task. It grants no additional account, worker, model, stage, concurrency or budget.

## Choose main once

```
TAUCETI_REBASE_REPAIR_TARGET=<PR>:<full-head>:<full-evidence-main>:current-at-launch
```

The evidence main stays bound to the original authenticated Bors v3 request, which Bors intentionally deduplicates by head. The fourth field may still be an explicit integration SHA, retaining the strict fixed-main behavior. The old three-field option remains rejected.

`current-at-launch` authorizes this precise rule: after acquiring the real branch claim, perform fresh authorization, read actual canonical main, verify it contains the original evidence main, and freeze that concrete SHA for the sole attempt. Earlier survey/dispatch checks may cheaply reselect main. Immediately before delegation, write the frozen SHA into both the prompt and an atomic launch receipt. Later main advances never retarget the task or replenish its budget.

## Existing guards and durable limit

Fresh direct reads require the exact PR/head/ref, open/nondraft status, named User author/fork, canonical main target, maintainer modification permission, actual canonical push permission, needs-rebase, no pause labels, and the original active authenticated Bors exact-head v3 request. Unknown API/ancestry, changed head, withdrawn request, unwritable source or divergent/rewound main refuses launch. Labels alone grant nothing.

A real existing branch claim is mandatory. Authentication and pause guards run again after the claim. A refused claim or failed authorization starts no task, writes no launch receipt and refunds the unstarted counter charge. If authorization is refused after acquisition, stop its heartbeat and release that matching claim before dispatch tries another candidate; leave any unrelated replacement claim alone.

The existing lifetime rebase-pr counter has a one-attempt cap. The same counter store writes `rebase-foreign-launch-<PR>.json`, containing PR, head, evidence main and integration main, with fsync/atomic replace before delegation. Its presence blocks another delegation after restart or stale selection, even if an unstarted preparation path refunded the numeric counter. Malformed/unreadable receipts do not grant retries. A crash after charging/recording can conservatively consume the scope without a paid task; report that instead of deleting state. Neither key depends on advancing main.

The agent fetches exactly the frozen SHA from canonical TauCeti, verifies FETCH_HEAD and integrates that commit, replacing ordinary origin/main fetch/merge instructions. It never integrates another PR branch or chases main. Original evidence is not called a fresh check against the integration snapshot.

Full build and axiom validation, human-owned path restrictions and the existing safe-push lease/head CAS remain mandatory. Live request/head/pause checks before push remain prompt requirements, as do source validation checks; no new mechanical build/label gate is claimed. Changed-head CI, math review and admission decide readmission. No-change or failure is not repair acceptance.

## Concrete proposal, still inactive

Existing worker2, one attempt on PR13940 head `dbba78bdb3a5f4d234373a4248a3022004331fa5`, original evidence main `03de080239e2f6ccee2f6e054e65c5445c50cf1e`, [authenticated Bors request](https://github.com/TauCetiProject/TauCeti/pull/13940#issuecomment-6206164978). The actual finding is a textual conflict in SymmetricFunctionalCalculus.lean, not missing-module evidence or proof of green head CI.

```toml
"TAUCETI_REBASE_REPAIR_TARGET" = "13940:dbba78bdb3a5f4d234373a4248a3022004331fa5:03de080239e2f6ccee2f6e054e65c5445c50cf1e:current-at-launch"
"PYTHONPATH" = "/home/kim/.local/share/tauceti/releases/<reviewed-code-commit>"
```

Prepare the exact inactive diff and baseline hash first. Preserve worker1's external changes and all other fields, including disabled science workers. Use an immutable reviewed release with state/checkouts/logs linked to existing locations; verify resolved state, checkout, logs, store and caches remain identical. Validate configuration and reconcile only the existing idle worker through its supported manager after explicit human authorization. Never overwrite newer settings with an old whole-file proposal or interrupt paid work.

Cost: at most one existing worker2 authoring attempt, using unchanged Codex/model/quota policy and default 90-minute cap, plus full source validation and ordinary fresh-head CI/review. No new paid worker, recurring service, dollar forecast or guaranteed repair. Cheap prelaunch reselection is not another paid task.

Rollback while idle removes only the repair target and restores the previously reviewed code path if needed, preserving later unrelated changes. Leave comments, labels, branches, claims, counters and the launch receipt intact. The old fixed-main239c question is obsolete and never enabled; it is replaced only by a reviewed concrete proposal with this explicit selection rule.

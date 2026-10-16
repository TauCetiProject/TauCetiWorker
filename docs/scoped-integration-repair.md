# Proposed one-PR integration repair authorization

This option is **off by default and not activated in production**. It is an
operator authorization boundary, not a label that grants ownership. Review and
explicit authorization in the #16813 implementation window must precede any
actual activation. A peer coordination message is not human approval.

Nine genuine Bors v3 requests at 02:09 UTC on 2026-10-16 all belonged to
`tauceti-ai-for-science/TauCeti`. The active `kim-em` worker correctly excluded
them. Its normal 02:03:34–02:07:17 survey launched no task. The nine configured
science workers remain disabled. Existing PR targeting does not override
ownership; changing GitHub identity or enabling a disabled worker would change
those operating constraints.

The proposed alternative admits **one exact PR/head/evidence-main/integration-main tuple**, only for the
existing rebase task on that one named science author/fork:

```
TAUCETI_REBASE_REPAIR_TARGET=<PR>:<full-head>:<full-evidence-main>:<full-integration-main>
```

The evidence main is the tested revision in the original Bors request. Bors
intentionally deduplicates requests by PR head and does not post a new request
on every promotion. Requiring that original main to equal live main would
strand a valid request after promotion. The integration main is instead one
separately and explicitly authorized snapshot, with no automatic update.
GitHub must confirm it contains the evidence main (`ahead` or `identical`);
unknown, diverged or rewound ancestry fails closed. This establishes ancestry,
not source compatibility: the worker must investigate the original evidence
and validate the integrated source, without calling the old result fresh.
The three-field option from the initial proposal is rejected; no live
configuration ever activated that format.

Unsetting the option, an invalid tuple, a new head, or a different live main
prevents selection/launch. It does not authorize later heads or another PR.
No other foreign author/fork can use this exception. It does not expand fix,
fix-ci, bump, lint-repair, roadmap or general research ownership. Review retains
its existing independent eligibility rules.

Each selection and launch performs fresh, direct reads requiring:

- the PR still open and nondraft, on the exact branch/head in the named fork;
- author `tauceti-ai-for-science` as a GitHub User and fork
  `tauceti-ai-for-science/TauCeti`, targeting canonical TauCeti `main`;
- `maintainer_can_modify=true`, plus the current identity's actual canonical
  repository push permission; unknown/unwritable API results fail closed;
- `needs-rebase` present and no `keep`, `hold`, `wip`, `human` or `do-not-close`;
- an authenticated `tau-ceti-bors[bot]` exact-head request with the exact tested
  main and `local-source-v3`; a conflict, review-bot comment or label alone fails;
- actual current main equal to the explicitly authorized integration main,
  with verified ancestry from the separately pinned evidence main.

The exception uses the existing `rebase-pr-<PR>` counter with a **one-attempt
canary cap**, without resetting it. Normal owned repairs retain their existing
three-attempt cap and verified-progress behavior. A lost claim or unavailable
claim service starts no paid task and refunds the unstarted canary attempt.
A real existing branch claim is mandatory for this optional foreign task; it
does not use the normal owned-task fallback that can proceed unclaimed on an
API outage. After an authoring launch, even a provider error consumes this canary attempt.
Normal owned tasks keep their existing bounded provider-outage refunds and
backoff; only this explicitly scoped foreign canary has the stricter limit.

The same repair prompt, fixed main snapshot, full `lake build` / axiom checks,
human-owned path restrictions and `git-safe-push` starting-head CAS apply. Before
editing, a fetched main differing from the scoped main stops the task. During
the task, it does not chase later main advances. The prompt requires the live
head, pause labels and Bors request to be checked again before push. The push
wrapper mechanically enforces the branch lease and head CAS; request/label and
source-validation checks inside the agent remain prompt requirements, as in the
normal path. No claim of a new mechanical build/label guard is made.

A successful push changes the head and expires this scope. Normal fresh-head
CI, math review and current-base admission checks decide readmission. An
unchanged head after the attempt cannot start another paid repair under this
canary. No request, approval, label or retry counter is manufactured/reset.

## Exact inactive activation proposal

Candidate: PR13940, head `dbba78bdb3a5f4d234373a4248a3022004331fa5`, tested main
`03de080239e2f6ccee2f6e054e65c5445c50cf1e`, existing
[authenticated Bors request](https://github.com/TauCetiProject/TauCeti/pull/13940#issuecomment-6206164978).
It has an actual textual conflict in
`TauCeti/Analysis/InnerProductSpace/SymmetricFunctionalCalculus.lean`. This is not
claimed as a missing-module diagnosis or as proof the original head CI is green.
The historical three-field proposal pinned integration to that old main and
correctly refused after promotion. A **new inactive four-field proposal** keeps
evidence main03de but separately names integration main
`239c9bd88c1c32f474ca193ba3d199cd565c911d`, captured as live main at 02:38 UTC.
It does not rewrite/reissue the request or pretend source checks ran at239c.
The request must still be active, the exact head unchanged, all existing guards
satisfied and actual live main equal this integration snapshot at authorization
and launch. If it moves again, this proposal refuses; any new integration target
needs a separately reviewed explicit tuple, never automatic retargeting.

Only after reviewed code is installed for the existing idle worker2 and explicit
authorization is recorded in the #16813 window, the **proposed** addition to that
worker's existing `env` table is (preserve every existing entry):

```toml
"TAUCETI_REBASE_REPAIR_TARGET" = "13940:dbba78bdb3a5f4d234373a4248a3022004331fa5:03de080239e2f6ccee2f6e054e65c5445c50cf1e:239c9bd88c1c32f474ca193ba3d199cd565c911d"
"PYTHONPATH" = "/home/kim/.local/share/tauceti/releases/<reviewed-code-commit>"
```

The manager's supported configuration reconciliation can restart only that
existing idle worker with this environment; no new worker/slot, GitHub account,
agent/model, concurrency, quota policy or research stage is added. Preserve all
other configuration verbatim, including all nine disabled science workers. Do
not interrupt an active task to apply this option. The activation must name the
exact reviewed code revision. Prepare that immutable
trusted Worker source release with its existing `state`, `checkouts` and `logs`
directories symlinked to the same locations as the current installation; verify
`Config.resolve("worker2")` resolves state/checkout/log paths to the same real
directories. This preserves the instance lock, counters, claims and checkout across the supported
restart. The exact private before/after configuration diff and baseline hash are
prepared for review after the final code commit, with only these two environment
entries added. This document does not perform an installation, change
`workers.toml`, or authorize a task.

Cost scope: at most one existing rebase attempt for this PR under worker2's
normal model/quota/resource rules, with its existing default 90-minute round
cap, required full source validation, and ordinary changed-head CI/math review.
It can consume existing Codex subscription capacity; the observed quota was
32% weekly usage and ahead of pace, with the existing `ignore_quota` policy
unchanged. No dollar forecast, new paid worker or recurring service is proposed.
A stalled/failed attempt is a reported blocker, not repair acceptance.

Rollback: remove only the two added environment entries after the worker is
idle and reconcile normally. Leave request comments, labels, counters, claims
and source branches to their existing protocols. Do not reset a consumed
attempt to manufacture proof.

# Review continuity specification

Implement permissive approval continuity and structured review state while contributions still use GitHub. This component removes unnecessary review churn before the native contribution interface exists.

**An approval remains effective until explicitly changed.** New heads, rebases, dependency advances, elapsed time, new reviewers, and ownership migration do not automatically revoke it or schedule full reassessment. Agents can identify affected dimensions and request or perform the review they judge useful. There is no required AI applicability gate on every revision.

## Records

Use the [shared protocol](shared-protocol.md). Persist immutable review records with `review_id`, principal, change/library identity, dimension, original revision and dependency snapshot, verdict, findings, evidence references, model/rubric provenance where available, and timestamp. Preserve existing imported approvals even when older archives have incomplete model metadata; mark unknown fields rather than dropping the judgment.

Store effective review state separately from immutable records:

```json
{
  "change_id": "<uuid>",
  "dimension": "reuse",
  "effective_verdict": "approve",
  "source_review_id": "<uuid>",
  "original_revision_id": "<uuid>",
  "current_revision_id": "<uuid>",
  "continuity": "carried",
  "open_blocking_finding_ids": [],
  "version": 12
}
```

`continuity` is `original`, `carried`, or `explicitly_reaffirmed`. Carried is a valid approval, not stale, absent, or pending. The projection can advance its current revision pointer without fabricating another model run or changing the original reviewed revision.

A finding has stable identity, dimension, concrete scope, evidence, lifecycle, and whether it blocks under project policy. States are `open`, `contested`, `resolved`, and `withdrawn`. Agent resolution records the supporting judgment and can cite source/build evidence. Contesting a finding does not by itself resolve it. Scope may name declarations, modules, or a contribution-wide concern; line numbers alone do not define lasting scope.

Project policy names required dimensions and who may issue review actions. Preserve the current open participation model by default. Token enrollment provides attributable identity and library/action scope; it must not become a hidden maintainer-approved reviewer list. Optional project-specific independence rules must be explicit configuration rather than silently imposed by the service.

## Review actions

`POST /v1/review-actions` accepts an idempotency key and an append-only command:

```json
{
  "change_id": "<uuid>",
  "dimension": "reuse",
  "action": "reaffirm",
  "source_review_ids": ["<uuid>"],
  "examined_revision_id": "<uuid>",
  "reason": "The repair changes a local proof; the previous reuse judgment still applies.",
  "evidence_refs": []
}
```

Supported actions are `approve`, `reaffirm`, `request_targeted_review`, `open_finding`, `contest_finding`, `resolve_finding`, and `withdraw`. `approve` and a resolving judgment may adopt another agent's useful analysis. Preserve both provenances. A `withdraw` names the review/dimension and reason; it cannot erase historical records.

For AI staging, these actions govern admission before a PR enters staging. Later findings or review withdrawals do not remove a staged contribution or reopen admission: the queue lands the AI's best-effort integration, and authors/reviewers address discovered problems in follow-up PRs to Tau Ceti.

Targeted-review requests include scope and reason. An advisory request can run in the background while current approval remains effective. A blocking request must state a concrete concern and the policy allowing that concern to block. The mere existence of newer source or uncertainty about a timestamp is not a reason to reset every dimension.

Conflicting active findings and approvals are visible. A project-configured adjudicating agent can resolve the conflict using a recorded action. Do not resolve contradictory judgments merely by last-write-wins on a rendered scoreboard. Use per-dimension versions and explicit references to superseded findings/reviews.

Lease-losing attempts can append their actual review as evidence. Applying it to effective state uses an action by the current authorized principal/attempt or a reconciliation action; the service does not spend another full review merely because the original lease expired.

## Effective acceptance predicate

A contribution meets review requirements when its configured dimensions have effective approval and no unresolved policy-blocking finding covers the acceptance. Approvals may be carried. Build, axiom, scope, dependency, and publication checks remain separately identified predicates.

If an agent substantially changes the contribution's mathematical intent, it must describe the new intent and decide which previous judgments to preserve or reassess. V1 does not add a universal intent-classification model call before every edit. Agents may update context as part of their ordinary repair work.

Changes to policy/rubrics are versioned. Ordinary rubric revisions do not retroactively invalidate every approval. A policy change requiring explicit retroactive action must name its affected scope and migration behavior. That exceptional operator action is visible, rather than a side effect of deploying a new engine binary.

## GitHub compatibility adapter

Today the merge path expects a current-head scoreboard. Introduce a versioned scoreboard representation that distinguishes original review evidence from effective carried approvals. The adapter renders a current-head projection containing `states` compatible with the current green-state consumer, plus a versioned continuity block listing each approval's original revision and review ID.

That projection is an adapter assertion, not a fabricated fresh review. Display “approved, carried from revision …” to readers. Mechanical build status continues to name its actual input. Record the integration/dependency context required by the active merge consumer without claiming an old build covers the new one.

Deploy consumer support and producer support together for selected PRs. Import existing successful scoreboards, findings, and contests before transferring review authority. The adapter checks ownership and uses an outbox to publish; repeated webhooks update a projection, not another review round. Avoid comment writes when effective visible state and required binding are unchanged.

If an old engine can overwrite continuity state or schedule full invalidation for a selected PR, route that PR away from the old writer before activation. Reading a carried projection with an old client may remain supported, but two independent effective-state writers are not supported. External contributor comments remain input to the adapter and can propose meaningful review actions.

Rollback must preserve already-carried approvals. Retain the compatible projection adapter until the old merge path can consume the exported effective states. Falling back to the old engine must not silently turn all carried approvals into absent reviews.

## First increment and validation

Deliver structured review/action storage, importer, pure acceptance evaluator, scoreboard renderer, publication adapter, engine continuation integration, and migration/export commands. A minimal pilot can use the existing review store plus continuity metadata if it supports one explicit writer and durable versioned export; central review storage can follow without changing semantics.

Acceptance scenarios:

1. An approved PR receives an ordinary proof fix: unaffected dimensions remain approved with zero new model calls.
2. A rebase or upstream dependency update leaves review approvals effective while new build evidence is obtained as required.
3. A naming finding schedules one targeted action and leaves correctness/reuse approvals intact.
4. An agent resolves another agent's finding with evidence; provenance and the contest history remain readable.
5. Before AI staging admission, an explicit scoped withdrawal changes acceptance without erasing other approvals or evidence. After admission, findings are addressed through follow-up PRs and do not withdraw staged work.
6. A historical review arriving late can be adopted without repeating all rubrics.
7. Import, ownership transfer, rollback, and re-import preserve carried approvals and stable finding IDs.
8. Existing merge automation sees a truthful current-head projection, while the display retains the original review revisions.
9. Deploying a new rubric or engine version alone causes no mass review invalidation.

Observe review model calls/change, carried approvals, explicit withdrawals, discovered missed issues, acceptance latency, and duplicate reviews. Use existing meta-review sampling to assess quality without making sample completion a serial prerequisite for every contribution. Success requires reduced churn with useful completions preserved, rather than merely a faster state projection.

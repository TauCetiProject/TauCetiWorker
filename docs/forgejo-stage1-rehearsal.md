# Stage 1 migration rehearsal

This records the isolated rehearsal against `GOAL.md` as of 2026-11-10. Production
development remains on GitHub. Stock Forgejo **17.0.1** provided the source server;
the public GitHub execution repository was
[`TauCetiProject/forgejo-stage1-rehearsal-20261109`](https://github.com/TauCetiProject/forgejo-stage1-rehearsal-20261109).
Fixture native IDs and credentials are separate from the intended real destination
`https://git.taucetiproject.org/TauCetiProject/TauCeti`.

**Stage 1 readiness is not yet declared.** Actual destination backup/recovery
access is still unavailable, and verification of the returned GitHub-mode controls
is running. The intended destination paths are chosen; operational ownership by
`kim-em` and real Forgejo site-administrator access are confirmed. No production import,
authority switch, mirror activation, fleet drain, or shared-state shutdown occurred.

## Six-gate record

| Gate | Evidence and remaining work |
| --- | --- |
| Destination | Actual operator access is confirmed and the real server reports 17.0.1. The fixture used a stock build; confirm the real build with host access. The intended organization/repository paths returned 404 at the access check, which does not prove absence. Bind provisioned native IDs and protections before publishing controls. Actual host backup/recovery access remains required. |
| Work preservation | Captured Git, queue, claims, spending, prepared publications, and Review stores were retained and recovery checked. A per-head transfer manifest names `kim-em` for decisions. Refresh and apply individual dispositions at the separately authorized cutoff; captured approvals never become native authority. |
| Contribution path | A failed real candidate was refused, a corrected candidate passed, stale head/base evidence was refused, and the final scoped dispatcher produced one verified owning child. Native review and the familiar contributor `gh` path were exercised. |
| Merge and outputs | The exact current candidate landed, only `main` mirrored through the App, main build/audits passed, the exact public cache remained usable, and actual API docs and Pages published the new native module. |
| Recovery | Durable handoff export, publication-only retry, real App mirror refusal/repair, scheduled catch-up, user-manager re-exec, isolated restores, and authority rollback preserving landed native work passed. Returned GitHub-mode build/publication verification is still running. |
| Handover | Executable [cutover](forgejo-stage1-cutover.md), [operator](forgejo-stage1-operator.md), [automation disposition](forgejo-stage1-automations.md), and opt-in [mirror units](../services/queue/stage1-mirror/README.md) are available. Complete the actual destination recovery record and final returned-mode verification before readiness sign-off. |

## Tested installed revisions

| Component | Immutable revision |
| --- | --- |
| Installed operator and mirror roles | Worker `e4ce95894d4f0e4ca3c7214d93eeba284d1b2bf8` ([#647](https://github.com/TauCetiProject/TauCetiWorker/pull/647)) |
| Operator's declared Tools dependency | `cc55ea7cbd272d688189c09230497d6a9f9ff101` |
| Operator's declared Review dependency | `6b07b400ba129a0f2771d2d315a410f72d462244` |
| Workflow harness and main/docs templates | Tools `08d5a676a9717444ce5a2bdd13f35a7c1ad9f40c` ([#216](https://github.com/TauCetiProject/TauCetiTools/pull/216)) |
| Installed standalone contributor `gh` client | Tools `9285c207f338d7c2e6b4464885b0027a85dbe659` ([#214](https://github.com/TauCetiProject/TauCetiTools/pull/214)) |
| Lean | Stock `leanprover/lean4:v4.36.0-rc2` |

The operator and Actions harness have independent pins. Adopting the harness did
not override the operator's declared dependencies. No Lean/Lake patch, Forgejo fork,
storage redesign, or new storage backend was needed for this supervised path.

## Candidate, promotion, and publication

The final request was `c557135b42571c020c5e47a12f5dc4cd`, ledger attempt 5:

- Accepted native PR head: `34ed5a5e864ad81e7ce92633388a25d02b5b911b`.
- Captured base/caller: `afef4b9395acfc28597c98490e1a76912dd39653`.
- Tested and landed candidate: `b505f6200393284c670fa542e530717941981637`.
- Candidate tree: `ab821a5e0216b34b960c46a74c2df63edb67a266`.

An installation token limited to this execution repository, `actions: write` and
`contents: read`, invoked exactly one
[protected parent](https://github.com/TauCetiProject/forgejo-stage1-rehearsal-20261109/actions/runs/42730220596).
The operator adopted the
[owning child](https://github.com/TauCetiProject/forgejo-stage1-rehearsal-20261109/actions/runs/42730269752)
through its verified receipt. Its full Lean build, finalizer, and required isolated
cache publisher passed. The token refused real writes to protected main and an
unprotected disposable branch; the observer also lacked repository write access.
Candidate execution received no source merge credential.

Both dispatcher and mirror grants came from the approved `tauceti-queue` App
(App ID **5607007**). The runtime dispatcher grant could not write contents, but a
holder of that App's private key can mint the mirror write role. Key holders remain
trusted administrators. Follow the cutover key-holder inventory and retire old
automated writer bindings; an App-only allowlist does not isolate holders of the
same App key.

Classic main push restrictions still admitted the repository administrator even
with admin enforcement and a nonempty App-only allowlist. The owned marker was
restored to the exact captured base. An active main `update` ruleset with only the
verified mirror App as bypass then refused the former bootstrap administrator.
Separate classic no-force and no-delete protections remained enabled. This policy
and its rollback are documented in
[#652](https://github.com/TauCetiProject/TauCetiWorker/pull/652).

After exact promotion, temporarily removing only the owned App's update bypass
produced a visible source-landed/mirror-refused result. Restoring the exact rule
allowed the unmodified timer, using a dedicated read-only native actor, to catch
up. Only GitHub `main` changed, and its tip equalled the tested candidate. The
schedule survived a user-manager re-exec and its next natural invocation succeeded.
The rehearsal timer was then stopped. Lingering was not changed; production host
startup/recovery duties remain in the operator's deployment record.

The App push triggered
[main CI](https://github.com/TauCetiProject/forgejo-stage1-rehearsal-20261109/actions/runs/42735434433),
which passed the build and all audits. It found the exact candidate cache map
already public and skipped duplicate staging/publication. Anonymous reads verified
the 28,275-entry revision map and the new module's archive against the candidate proof.
The map SHA-256 was
`12fa10a0fa4ee61d7cfbcb03c259c13b0fb7f3502699f100c34312d781fff8f0`.

[API docs](https://github.com/TauCetiProject/forgejo-stage1-rehearsal-20261109/actions/runs/42735462564)
passed and saved HTML and incremental analysis caches.
[Pages](https://github.com/TauCetiProject/forgejo-stage1-rehearsal-20261109/actions/runs/42738154471)
built its data and site, then timed out in `syncing_files`. A deployment-only
recovery used the same artifact **13729214840** and succeeded in 26 seconds after
waiting for a runner. Producer job and step timestamps were unchanged: data, site,
and API builds were not repeated for that retry.

Anonymous HTTP checks verified the landed `SOURCE_SHA`, the new
`TauCeti.StageOneMigration` page, an existing module, index pages, and stylesheet.
The complete Pages run remained red because the empty-history execution fixture
could not regenerate two historical PR charts. Its deployment succeeded; the
chart report finished before a proposed retirement cancellation, so no cancellation
was sent. Live publication URLs can advance after these captured checks.

## Preservation and rollback

The 2026-11-09 private snapshot record retains 2,189 captured open PR heads,
6,357 branches, ten tags, queue refs and active batch records, claims, spending,
prepared publications, and canonical and worker Review stores. Git bundles and
database/file snapshots were restored or integrity checked as appropriate. These
are preparation snapshots, not a claim about the live fleet at cutover. The
per-head dispositions remain proposals until the operator settles them against
the final cutoff inventory; no paid output was replayed or approval imported.

Rollback archived the owned native repository and proved its former writer could
not push. Every source ref was retained; the stopped mirror reader could still
perform the final equality confirmation. A fresh isolated stock restore preserved
the archive state, closed PR/head, revision-bound native review, native IDs, Git
metadata, and protections, with database integrity `ok`.

Only after that fence did the administrator transfer GitHub write ownership,
remove the recorded repository update ruleset, retain no-force/no-delete
protections, and install the reviewed GitHub controls using an expected-old push.
The returned main `753b59c1c9b7302f41784ace50b8758a9f4164f8` is a direct child of
the landed native candidate. It changes only control/configuration files; the
native Lean contribution and dependency bytes are unchanged. Native source opt-in
is explicitly false and original API/Pages workflow active states are restored.
The recorded post-return classic policy allows `kim-em`, with no App or team
exception and with admin enforcement, no-force, and no-delete retained. A real
fast-forward push by the former mirror App was refused by GitHub's protected-branch
hook, leaving the returned main unchanged.

The old mirror command subsequently refused to overwrite the returned GitHub
history. The native source stayed archived at its landed tip with every ref
unchanged. The original five CI attempts remained in the same ledger, without
resending a parent or resetting work. The old attempt 3 already had a verified
terminal source failure and refused promotion. Its historical local handoff row
was explicitly retired through `abandon` during the post-rollback evidence audit.
The final ledger backup retains attempts 1–4 as superseded and attempt 5 as passed.

The returned controls triggered
[main CI](https://github.com/TauCetiProject/forgejo-stage1-rehearsal-20261109/actions/runs/42750580933),
[API docs](https://github.com/TauCetiProject/forgejo-stage1-rehearsal-20261109/actions/runs/42750580656),
and [Pages](https://github.com/TauCetiProject/forgejo-stage1-rehearsal-20261109/actions/runs/42750580720).
Their final verification is pending in this record.

## Private evidence index

Keep these artifacts with the operator's private deployment record, alongside the
original configuration, profiles, test commands, manifests, and backups. The
public CI links above supplement that retained evidence; the record is not a
request to publish private worker or credential stores.

| Claim | Retained evidence |
| --- | --- |
| Scoped grant and single protected parent | `workflow-dispatch-role-grant-proof.json`, `final-role-parent-invocation-intent.json`, `final-role-parent-invocation-response.json`, `final-role-parent-final-jobs.json` |
| Scheduled mirror and manager re-exec | `final-mirror-timer-catchup-proof.json`, `final-mirror-manager-reexec-proof.json`, `final-mirror-post-reexec-second-invocation-proof.json` |
| Native archive/write refusal and restore | `native-rollback-fence-proof.json`, `rollback-archived-source-raw-refusal-proof.json`, `backup-rehearsal-native-fenced-20261110-proof.json` |
| Expected-old return, exact parent, and post-return policy | `return-github-authority-reconciled-intent.json`, `return-github-authority-push-response.json`, `reconcile-return-github-authority.py`, `rollback-controls-prepared.json`, `rollback-github-control-commit.json`, `rollback-protection-after.json`, `return-github-authority-proof.json`, `rollback-app-push-refusal-proof.json` |
| Failed handoff retirement and retained requests | `failing-child-terminal-observation.json`, `failing-promotion-refusal.json`, `retired-failed-handoff-closure.json`, `operator-rollback-final-ledger-proof.json` |
| Git, queue, claims, spending, and prepared work | `active-git-backup-proof.json`, `queue-git-backup-proof.json`, `git-claim-inventory-proof.json`, `worker-accounting-proof.json`, `prepared-publication-proof.json`, `retained-source-proof.json`, `review-store-proof.json`, `worker-review-store-proof.json`, `active-work-transfer-manifest.json` |
| Historical chart diagnostics and published output | `post-native-pages-final-jobs.json`, `post-native-pages-final-chart-report.log`, `post-native-public-pages-proof.json`, `post-native-pages-recovery-proof.json` |
| Baseline artifact size and existing production comparison | `pages-primary-artifact-inspection.json`, `pages-existing-production-comparison-proof.json` |

## Remaining readiness decisions

Obtain actual Forgejo host/backup access, verify a usable recovery path for its
Git/database/configuration, and bind the intended destination's provisioned native
identities and protections. `kim-em` owns administration, backup/recovery, review,
dispatch, controlled merge, mirror operation, and work-transfer decisions; separate
role holders are not required. Production cutover remains a separate decision.

Retain the captured Pages hosting risk in the handover: the baseline site artifact
contained about 1.72 GB of regular files against GitHub's documented
[1 GB published site limit](https://docs.github.com/en/pages/getting-started-with-github-pages/github-pages-limits).
A [larger existing production artifact](https://github.com/TauCetiProject/TauCeti/actions/runs/42688429884)
also deployed successfully, so
size was not established as the timeout's cause. Deployment-only recovery worked
for both baseline and post-native output. This does not authorize a hosting,
minifier, or storage redesign.

Autonomous Review/Progress, queue and Fleet completion, broad GraphQL compatibility,
capacity testing, and shared-state retirement remain Stage 2 or separately tracked
work. They were not substituted for these Stage 1 gates.

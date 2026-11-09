# Stage 1 cutover and rollback

This procedure moves the main development repository to Forgejo with human
review, CI dispatch, and merge ownership. GitHub remains the CI/cache/docs
execution home and a `main` mirror. It does not enable the Stage 2 autonomous
Worker/Review/Progress lifecycle or retire the shared GitHub state service.

Preparation and isolated rehearsal are authorized by [GOAL.md](../GOAL.md).
Production cutover requires a separate explicit authorization of the completed
deployment record below. Do not execute its authority changes during preparation.

## Deployment record

Complete this record before requesting cutover authorization. An unset value or
an unproven essential rehearsal result means migration readiness is incomplete.

| Item | Required record |
| --- | --- |
| Source | Forgejo origin, organization/path, native repository ID, default branch, supported server version |
| Execution | GitHub owner/path and native repository ID; default branch `main` |
| Operators | Named human reviewers, their native Forgejo IDs, dispatcher, merge operator, and cover for absence |
| Recovery | Named Forgejo administrator and backup/recovery owner; access to Git, database, configuration, keys, and tested restoration |
| Credentials | Human source-token files, dedicated mirror-reader account/profile, read-only CI observer, narrowly scoped GitHub workflow dispatcher, execution App directory, installation/repository selection, and main push permissions |
| Controls | Reviewed configuration/policy bytes, immutable Worker/Tools revisions, protected caller/control commit, and expected old tips |
| Existing work | Timestamped PR/branch/tag inventory, claim namespaces, queue records, spending history, prepared outputs, and a disposition/owner for unsettled work |
| Automation | Old source writers to fence, retained execution jobs, temporary manual duties, and the mirror repair schedule |
| Evidence | Real failing/fixed CI, stale-head/base refusal, exact promotion, App-triggered mirror, main cache/docs outputs, interruption repair, backup and rollback |

The implementation baseline is Worker
[`e4ce95894d4f0e4ca3c7214d93eeba284d1b2bf8`](https://github.com/TauCetiProject/TauCetiWorker/pull/647)
and Tools
[`d43efb27dd27e8dc0b7b36f31320b8c069ff7a1a`](https://github.com/TauCetiProject/TauCetiTools/pull/213).
Record any subsequently adopted reviewed revisions explicitly. The operator and
workflow harness have independent installations/pins; installing a new Actions
harness does not authorize overriding the operator's declared dependencies.

Stock Forgejo 17.0.1 is the rehearsal baseline. Verify the actual destination
version and capabilities; do not deploy a server fork as an implicit prerequisite.

## Prepare without transferring production authority

1. Confirm native permissions and recovery ownership. Test contributor fork/push,
   source main and retained-ref protection, and an authorized operator on the
   intended supported server. Disable Forgejo Actions on the source repository:
   its `.github/workflows` are execution-mirror callers, not native runner jobs.
   CI execution never receives source merge credentials. Require operator
   `execution_mode: "workflow"` for this cutover; App-mode CI would share the
   mirror App's main branch exception. Test that the dispatcher's workflow-only
   credential and the CI observer cannot push GitHub main.
2. Preserve history and active work. Keep exact Git objects for required branches,
   tags, dependency pins, PR heads, and queue candidates. Verify the self-contained
   bundles and restore them independently. Keep historical GitHub discussions and
   the correspondence between old GitHub objects and newly created native objects.
   Use protected archival branches such as `migration/github/pr-N` and
   `migration/github/queue-ID` for retained old heads. Do not push into Forgejo's
   server-managed `refs/pull/*` namespace or treat archived heads as native approvals.
3. Export existing ledgers with their online backup/read-only interfaces. Preserve
   the authoritative spending ledger and prepared publication bodies/assets, not
   just summary counts. Include Git claim refs and objects in every namespace used
   by the affected workers. Unknown or unavailable records need reconciliation;
   they are not evidence that work or claims are absent.
4. For each unsettled item, choose an owner and one disposition: finish against the
   old GitHub authority, preserve for an explicit native transfer, or retain pending
   investigation. Reuse prepared code/review output without repeating paid work.
   A transferred head needs fresh native acceptance and appropriate CI; old GitHub
   approvals, claims, numbers, and run IDs do not become native authority.
   Withdraw/close transferred items in the old queue/GitHub workflow with explicit
   correspondence before any rollback can reactivate that queue. Retain the old
   records; a restarted controller must not re-admit work that already landed natively.
5. Prepare the Stage 1 operator profile and protected callers using the
   [operator instructions](forgejo-stage1-operator.md). Retain the old queue database
   and artifacts. Use the existing ledger implementation with an explicit native
   source binding; do not relabel an old GitHub ledger's authority.
6. Generate/review only the essential main CI, API-docs, and Pages workflows using
   the pinned Tools release. The flagship generator requires `features.docs = true`,
   the existing `web/`/`docbuild/` layout, and matching source/execution branch names.
   Supporting Roadmap/tools/dependencies may remain on GitHub. Identify and record
   obsolete GitHub source writers for the authorized switch using the
   [automation disposition](forgejo-stage1-automations.md). Historical GitHub
   analytics must remain identified as historical until their Stage 2 replacements
   are ready.
7. Match cache endpoint variables to the trusted configuration. Pass the upload key
   only to the isolated reusable publisher. Required publication must fail on
   missing configuration. Confirm published revision maps and usable documentation
   with a well-formed `SOURCE_SHA`; generated YAML and synthetic success are not
   publication evidence.
8. Rehearse this entire procedure in isolated destinations, including rollback
   after a contribution has landed. Retain exact commands, expected tips, results,
   and interrupted-operation recovery evidence in the deployment record.

Normal production worker and CI admissions stay enabled throughout preparation.
Inventory snapshots must be refreshed at the actual authority transfer. Do not
drain the whole fleet to make a snapshot or install an operator tool.

## Preservation commands and checks

Create a new private backup directory, never a shared public evidence directory:

```sh
umask 077
mkdir -m 700 "$backup"
services/queue/backup.sh --database "$old_queue_database" --dir "$backup/queue" --keep 14
git -C "$content_clone" bundle create "$backup/content.bundle" --all
git -C "$content_clone" bundle verify "$backup/content.bundle"
git bundle list-heads "$backup/content.bundle" > "$backup/content-refs.txt"
```

The queue backup command verifies SQLite integrity. Record the actual returned
filename, hashes, and schema/table counts. Verify the bundle in a new independent
clone, including queue candidate/submission refs and exact imported PR heads;
main history alone does not preserve all prepared work. Capture other claim
repositories and contributor fork heads in their own bundles with exact ref/OID
manifests. The current operator host uses `~/TauCetiQueue-data/queue.sqlite3` and
`~/TauCetiQueue-data/clone`; verify the installed configuration rather than relying
on those historical path names.

For file stores, use explicit inventory-approved roots and relative path lists,
preserving bundles, patches, prepared output and publication bodies together:

```sh
tar -C "$worker_state_root" --one-file-system -czf "$backup/worker-retained.tgz" \
  -T "$deployment_record/worker-retained-paths.txt"
tar -C "$review_store" --one-file-system -czf "$backup/review-store.tgz" \
  ledger.json reviews outbox
tar -tzf "$backup/worker-retained.tgz" > "$backup/worker-retained-files.txt"
tar -tzf "$backup/review-store.tgz" > "$backup/review-store-files.txt"
sha256sum "$backup/content.bundle" "$backup/worker-retained.tgz" \
  "$backup/review-store.tgz" > "$backup/SHA256SUMS"
```

Do not dereference credential symlinks or sweep worker homes/build caches into
these archives. A tar exit reporting changed files is not an atomic successful
snapshot; retain the result for diagnosis and refresh after the approved project
cutoff/checkpoint. The current canonical Review store is
`~/.cache/tauceti-review/store/TauCetiProject__TauCeti`; include custom worker stores
and their original GitHub publication identities as well.

Capture `~/.local/state/tauceti/budget/events.jsonl` under its existing shared
`budget.lock`, without repairing or rewriting the live ledger. Validate its full
sequence using the installed `Budget.records`/`replay` methods on the private copy.
Back up `activity.sqlite` through SQLite's online backup API and run
`PRAGMA integrity_check` on the copy. Capture the existing GitHub publication
outboxes under both configured shared-state and legacy data roots. These commands
and the worker path list must be resolved for every affected host in the deployment
record; known defaults are not proof that custom roots are absent.

The Forgejo recovery owner supplies the instance-specific Git/database/configuration
and key backup commands and performs an isolated restore with native IDs, PRs,
protections, and all refs verified. A Git bundle by itself is not an instance
backup. Preserve application configuration/keys privately and exclude the restored
instance from production webhooks, mirrors, runners and notifications.

## Authorized authority transfer

The human-operated Stage 1 mode temporarily replaces autonomous source operations
for this project. Confirm its staffing and treatment of existing paid work in the
cutover authorization. If uninterrupted autonomous operation is required, explicitly
promote and complete the relevant Stage 2 prerequisites before this operation.

1. Apply the explicitly authorized per-project intake cutoff from the deployment
   record before settling in-flight work. Record its timestamp and prevent new
   TauCeti GitHub authoring/claims and queue admission; leave unrelated projects
   and CI running. The record must include the concrete configuration change for
   the currently installed worker/queue revisions. Do not invent an unimplemented
   pause flag or silently substitute a fleet drain. Decide whether the current
   queue head finishes under GitHub authority or is retained for transfer, and
   let paid repair sessions checkpoint/submit before stopping their workers.
   Refresh work/claim/publication inventories and backups after that boundary.
   Preserve original refs, identities, spending history, and prepared outputs.

   The installed manager supports persistent per-worker disable through
   `tauceti workers --config PATH disable ID`. Record the explicit configuration
   path and approved affected worker IDs; the operator host currently uses
   `~/.config/tauceti/workers.toml`. Confirm its source bindings at cutover rather
   than selecting every worker by name. For each affected worker, after its paid
   session has checkpointed or finished under the recorded disposition:

   ```sh
   tauceti workers --config "$workers_config" status --json
   tauceti workers --config "$workers_config" disable "$affected_worker_id"
   tauceti workers --config "$workers_config" status --json
   ```

   `disable` persists stopped state and the manager stops that worker; it is not
   a graceful inference-checkpoint command. Do not use it to interrupt unpreserved
   paid work. Repeat only for the authorized project workers, verify both stopped
   desired state and actual process exit, and retain their stores/outboxes. Keep
   unrelated workers and CI enabled. These are authority-transfer commands, not
   preparation or routine deployment precautions.
2. Fence the old GitHub merge/source writers for the affected project before
   enabling Forgejo merge authority. The old queue and new mirror can share a
   GitHub App identity: different token permission subsets do not distinguish
   their branch-protection exceptions. A still-running old queue must not be able
   to merge onto the mirror. The current operator-host queue unit is
   `tauceti-queue.service`. Persist publication off while its admin endpoint is
   still reachable, then disable and stop the service:

   ```sh
   tauceti-queue settings --url "$old_queue_url" set publication_enabled false \
     --token "$old_queue_admin_token"
   tauceti-queue settings --url "$old_queue_url" show
   systemctl --user disable --now tauceti-queue.service
   systemctl --user is-active tauceti-queue.service
   systemctl --user is-enabled tauceti-queue.service
   ```

   Confirm persisted `publication_enabled: false` before stopping, then the
   expected inactive/disabled results. Record shadow mode when testing a restart.
   Set the file seed false as well; do not delete the persisted setting/ledger.
   **Do not run `services/queue/install.sh` on the fenced host:** it enables and
   starts this service even with `--no-restart`. Leave a durable host/deployment
   notice and the same restriction in the queue operations record.

   After their paid work is settled, disable the installed repair units
   `tauceti-queue-repair.service`, `tauceti-queue-repair@2.service`, and
   `tauceti-queue-repair@3.service` on their actual hosts. Fence the workflow
   backstop and classify/disable installed source-related workflows including
   `update.yml`, `update-upstreams.yml`, `toolchain-tags.yml`, `auto-merge.yml`,
   `merge-sweep.yml`, `merge-only.yml`, `deprecated-prs.yml`, `merge-conflicts.yml`,
   `housekeeping.yml`, and `progress-merge.yml`. Keep the old `tau-ceti-bors`
   installation suspended. Do not execute the older Bors rollback procedure while
   Forgejo owns merges. Confirm the actual workflow/unit inventory rather than
   assuming every listed historical name is still installed.

   Account for late old-work publications and route new
   source work through the native human workflow. This is a project authority
   transfer, not permission to drain unrelated workers or CI pools.
3. Capture the final old main and import/update the required Git objects using
   expected old tips. Verify source/native IDs, branches, tags, pins, and exact
   commit IDs. If final main advanced since rehearsal, regenerate the bootstrap
   commit on that captured tip. Verify its diff contains exactly the reviewed
   control changes, and that control paths have not drifted since the reviewed
   base. Unexpected drift stops the cutover for review; it is not an automatic
   rebase approval. Record the final commit SHA and expected-old tips, then update
   the private profile's configuration/policy and caller anchor to those final
   bytes. Install that commit on both protected mains using the named source and
   GitHub bootstrap writers in the authorization. The controls must agree with the
   private policy/configuration and the profile's `workflow_revision` anchor.
   Ordinary `prepare`/`promote` commands deliberately refuse workflow/control
   changes; do not waive that check to deploy controls.
4. Set source protections so only the approved operator can advance main and
   retained candidate refs. Set GitHub main protections to permit the intended
   mirror App, and remove obsolete writers/exceptions after the reviewed bootstrap.
   Confirm that a writable contributor cannot update either protected source ref.

   Install the mirror App exception before removing the temporary GitHub bootstrap
   writer. Require admin enforcement and a nonempty App-only push allowlist: no
   human users or teams, and exactly the approved mirror App. An empty actor list
   is not a verified deny-all policy: the isolated negative test allowed the
   administrator to push even with `enforce_admins` enabled. Read effective settings
   with an administrator credential, separately from the CI observer:

   ```sh
   gh api --hostname github.com \
     "repos/$execution_owner/$execution_name/branches/main/protection" \
     --jq '{enforce_admins:.enforce_admins.enabled,users:[.restrictions.users[].login],teams:[.restrictions.teams[].slug],apps:[.restrictions.apps[].slug]}'
   ```

   In the isolated rehearsal, verify actual main push refusal for the dispatcher
   and observer and unchanged main tips. A settings read, an empty allowlist, or
   a Git dry run is insufficient. Keep this gate incomplete until the App-only
   policy has passed; do not call the temporary bootstrap configuration ready.
5. Confirm the mirror App is installed on the exact execution repository and can
   mint the scoped `contents: write`/`workflows: write` token. Set `mirror_main: true`
   and confirm the equal bootstrap tips:

   ```sh
   python -m tauceti_queue.stage1 --profile "$profile" mirror
   ```

   The initial/repinned-anchor case only confirms equality; it cannot push an
   unexplained bootstrap difference. Reconcile any mismatch before proceeding.
6. Announce the approved source home through the separately authorized channel.
   Give contributors the native clone/fork URLs and project-scoped `gh`
   compatibility configuration. For an existing clone, after preserving local
   branches and verifying the old remote, use the native fork as `origin` when
   contributing through a fork. Use the canonical native repository only for an
   account with the intended direct-push permission:

   ```sh
   git remote rename origin github
   git remote set-url --push github disabled://github-mirror
   git remote add origin "$native_origin_clone_url"
   git fetch origin
   git branch --set-upstream-to=origin/main main
   ```

   Preserve an existing GitHub `upstream` remote under another name before adding
   the canonical native repository as `upstream`; do not discard local branches
   or refs during the change. Native PR numbers and actor IDs belong to the new origin. Keep old links as
   historical references and create explicit transfer correspondence.
7. Admit a first native contribution through explicit current-head review and
   the operator commands below. Verify the exact tested candidate on both mains,
   a real App push-triggered GitHub run, cache publication and usable docs. Check
   useful completions and reconcile any interrupted or stranded old work.

## Routine human-operated contribution

Use `python` from the verified pinned operator installation. Set
`execution_repository` to `github.com/OWNER/REPO` so CI commands select GitHub even
when the project's familiar `gh` command uses the native compatibility layer.

The named reviewer first reviews the current native head and policy. Then:

```sh
python -m tauceti_queue.stage1 --profile "$profile" prepare \
  --pr "$native_pr" --accept-head "$reviewed_head"
python -m tauceti_queue.stage1 --profile "$profile" handoff \
  --attempt "$attempt" --out "$private_directory/input.json"
gh workflow run stage1-dispatch.yml --repo "$execution_repository" \
  --ref main --json < "$private_directory/input.json"
```

Invoke the parent once. Retain its run ID and intent; a lost response does not
authorize resending. Verify/adopt the child through its trusted receipt, then
observe its current result:

```sh
python -m tauceti_queue.stage1 --profile "$profile" observe \
  --attempt "$attempt" --run-id "$child_run"
python -m tauceti_queue.stage1 --profile "$profile" promote --attempt "$attempt"
```

Only a current passing result and acceptance can land the exact candidate against
its captured base. Promotion automatically attempts the main mirror. The human
operator verifies both main tips and required outputs; a source-landed/mirror-failed
message requires mirror repair, not another merge. Use the documented `mirror`
command for scheduled repair with a read-scoped source profile and the same reviewer
ID list and ledger. The scheduled profile must be mirror-only with disjoint
`mirror_actor_ids`, backed by an account with no source repository write role.
Use the reviewed release that implements that field; older releases reject it.
The operator host remains trusted: command roles do not isolate untrusted code
running as the same OS user. Candidate execution never receives these files.

Use the opt-in [mirror service/timer](../services/queue/stage1-mirror/README.md).
Before readiness sign-off, set its actual absolute executable/profile paths and
named ownership, install it in the isolated rehearsal, and prove invocation,
an actual catch-up push, and recovery after a user-manager restart or reboot.
Record lingering or the staffed host restart duty. Its reviewed files, schedule and enable/disable commands belong
in the deployment record. Activate production scheduling only at the authorized cutover.

## Abort criteria

Stop before announcement if identity, backup, protection, credentials, control
bytes, workflow checks, or mirror equality differ from the reviewed record. Leave
both histories and ledgers intact, keep both writers fenced while settling a
partial deployment, and follow the reviewed return-to-GitHub procedure. Do not
enable the old writer merely because a preparation command failed.

After native contributions begin, any rollback must preserve all landed native
work and reconcile pending sends first. Do not discard newly created PR heads or
restore an old main tip to make equality checks pass. Unavailable evidence never
authorizes advancing either main.

## Repair and rollback

Use the [operator recovery commands](forgejo-stage1-operator.md) for uncertain
dispatch, latest-attempt observation, abandonment, and lost promotion responses.
Preserve refs and records while reconciling. Exit `77` is unavailable evidence;
exit `78` is refusal. Neither authorizes a merge. A divergent GitHub main needs
an authority decision; the mirror refuses to force it backward.

If returning development authority to GitHub:

1. Disable the mirror timer and wait for any current service invocation to finish.
   Fence native promotions using the reviewed Forgejo protection/token revocation
   commands in the deployment record. Remove source write access from human
   operator credentials while retaining the dedicated mirror reader. Keep ordinary
   unrelated workers and CI enabled. Reconcile any existing promotion intent before
   abandoning unpromoted requests with the operator's `abandon` command; do not
   edit pending rows to make them disappear.
2. Capture the latest native main, PR heads, retained candidate refs, database,
   configuration, and pending publication/spending records. Record every contribution
   landed after cutover. Preserve both sides before reconciling any mirror lag.
3. With native source writes fenced, run the final `mirror` command to reconcile
   any lag, verify identical main tips, then stop the mirror service using the
   recorded unit name. Its timer must remain disabled. If divergence prevents this, preserve both
   histories for an explicit reconciliation; never force away either side.
   Ensure the returning GitHub main contains all post-cutover landed work. The
   expected common case keeps the current mirrored tip. Do not reset to the
   pre-cutover backup and discard the new contributions. If recovery uses an older
   backup, restore the retained post-cutover Git objects and reconcile them first.
4. Deploy the reviewed GitHub-mode controls on that retained history using expected
   old tips. Transfer protection/credential ownership back to the approved GitHub
   writer only after the native writer and mirror have been fenced. Verify the
   GitHub provenance readers still reject qualified native `Source-PR` citations
   as GitHub-number authority before re-enabling source automation.
5. Move unsettled native contributions by explicit head/URL correspondence and
   fresh GitHub review/CI. Retain native discussions and prepared work; do not
   reinterpret native approvals as GitHub approvals or repeat paid inference.
   Freeze the canonical Forgejo repository through its reviewed archival/protection
   procedure and point contributors back to GitHub. Restore the intended push URL
   and branch upstreams explicitly; renaming remotes alone leaves old tracking.
6. Restore the old source configuration and approved project automation, including
   the old queue only after GitHub has sole merge authority. Verify useful
   completions, build/cache/docs identities, claims, and stranded publications.

Actual production rollback needs an explicit operator decision. The isolated
rehearsal must already demonstrate preservation of a post-cutover contribution;
this document alone is not that evidence.

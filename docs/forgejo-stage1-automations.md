# Stage 1 automation disposition

This is a cutover checklist for the main TauCeti repository. It does not disable
production workflows during preparation. GitHub remains the execution repository;
Forgejo owns source reviews and merges. Disable native Forgejo Actions on the
source repository so its mirrored execution callers do not run there.

The inventory below covers all 38 workflow files at TauCeti commit
[`ed6c0aa3acc0a3cb56901dc406167cdbad593f59`](https://github.com/TauCetiProject/TauCeti/tree/ed6c0aa3acc0a3cb56901dc406167cdbad593f59/.github/workflows).
Refresh it against the actual cutover revision and the execution repository's
registered workflow IDs. It is a proposed disposition, not a record that any
production workflow has already changed.

## Minimum execution set

Retain reviewed, pinned native-source replacements for `ci.yml`, `api-docs.yml`,
`pages.yml`, and `pr-build.yml`, plus the protected manual-dispatch parent recorded
in the deployment record. Use the Tools main-mirror generator and source-candidate
contracts; keeping an old workflow filename does not make its old implementation
compatible with Forgejo.

The current rehearsal calls reusable build/cache workflows in the immutable Tools
release on GitHub. Enumerate every transitive workflow/action reference before
fencing local helpers: a workflow still used by an approved caller must remain
available. Supporting Tools/Review/Roadmap repositories are outside this source
repository's workflow fence.
Any supporting-repository job that writes or authorizes changes to the migrated
source must also be fenced. Keeping its Git hosting does not make it a source
authority after cutover.

| Existing workflow files | Stage 1 disposition and replacement |
| --- | --- |
| `ci.yml`, `api-docs.yml`, `pages.yml` | Replace with reviewed qualified main-mirror callers. Prove actual builds/cache/docs outputs after an App mirror push. Pages' historical GitHub analytics remain identified as historical; its old docgen branch writer stays disabled. |
| `pr-build.yml` | Replace with the protected exact-source-candidate caller. Humans prepare/dispatch/observe/promote through the operator; native PR numbers never become GitHub PR numbers. |
| `pick-runner.yml`, `publish-lake-cache.yml` | Keep only if referenced by the approved execution graph with compatible trusted configuration. The rehearsal uses pinned Tools reusable workflows instead of these legacy local definitions; fence unused registered local workflows. |
| `cancel-bors-candidate.yml`, `full-lint-label.yml`, `pr-status.yml`, `retry-ci-status.yml`, `fleet-recovery.yml`, `pr-labels.yml` | Fence legacy GitHub PR/Bors dispatch, cancellation, statuses, reruns, and recovery. Operator commands verify the source request/run/attempt and recover the owning child. Do not let a legacy timer select unrelated mirror runs or native PR numbers. |
| `claims-access.yml`, `housekeeping.yml`, `review.yml`, `pr-profile.yml`, `stack-policy.yml`, `stack-policy-wake.yml`, `roadmap-label-reconcile.yml`, `labels-from-comments.yml`, `deprecated-prs.yml`, `merge-conflicts.yml`, `update.yml` | Fence old source mutations and GitHub PR policy machinery. Humans own native review/labels, scope/dependency checks, conflicts, maintenance proposals, and active-work disposition until the corresponding Stage 2 integration passes. |
| `lint-full.yml`, `nightly-verify.yml`, `warm-dependencies.yml`, `shadow-audits.yml` | Defer these legacy scheduled/manual paths until qualified and rehearsed. `lint-full` includes a repair PR writer. Preserve required build/audit/cache/dependency checks in the approved candidate/main path; absence of an equivalent required check blocks that class of contribution. |
| `ci-live-summary.yml`, `fleet-smoke.yml` | Optional execution diagnostics. Default to fenced in the minimal migration; re-enable only after source/execution routing, output destinations, and runner behavior are verified. Native GitHub run/job/log commands remain available to operators. |
| `ci-live-tests.yml`, `hosted-cap-contract.yml`, `infrastructure-tests.yml`, `test-labels-from-comments.yml`, `website-tests.yml` | These existing infrastructure/test paths need their own compatibility decision. Default to fenced until tested against the new configuration; keep support-repository CI running. Any check required for a proposed infrastructure/control change must have equivalent verified evidence before that change is admitted. |
| `stuck-alerts.yml`, `toolchain-tags.yml`, `zulip-healthcheck.yml`, `zulip-pr-backfill.yml` | Fence legacy notifications and GitHub-source reporting. Record temporary human monitoring duties. No test notifications go to production destinations. |

Fencing automation is not a policy waiver. If a contribution needs a specialized
check that the Stage 1 path cannot perform, hold that contribution until its
required evidence is available through an approved supervised path. Do not import
old GitHub approvals/statuses, trust editable PR text, or merge through the web UI
to get around the missing check.

## Apply only during the authorized authority transfer

Record `execution_repository` as `github.com/OWNER/REPO` and verify its native ID
against the deployment record. Capture current states and resolve the reviewed
filenames to workflow IDs in that exact repository:

```sh
gh api "repos/$execution_owner/$execution_name/actions/workflows" --paginate --slurp \
  --hostname github.com \
  > "$deployment_record/github-workflows-before.json"
gh workflow list -R "$execution_repository" --all
```

For each approved obsolete workflow ID, disable it and read back its state:

```sh
gh workflow disable "$obsolete_workflow_id" -R "$execution_repository"
gh api "repos/$execution_owner/$execution_name/actions/workflows/$obsolete_workflow_id" \
  --hostname github.com --jq '{id,path,state}'
```

Require `disabled_manually`, and separately inspect already queued/running old
source-writing jobs. Disabling a workflow does not cancel an existing run. Settle
or explicitly cancel those writers for the affected project, preserving their
prepared outputs and uncertain outcomes, before handing source authority over.
Fence the old queue/controller as described in the cutover runbook as well;
workflow state alone does not fence its App identity.

After the mirror push, read the registered workflow states again, verify that
only the intended essential runs started, and verify the retained historical
GitHub PRs were not mutated by obsolete automation. New workflow files or missing
IDs require review; do not infer that a filename mismatch is harmless.

On rollback, preserve the post-cutover native work and fence native promotion
first. Re-enable only the old workflows selected by the rollback record, after
settling transferred/landed GitHub work and restoring one merge authority. Do not
bulk-enable old source writers while the Forgejo authority or mirror is active.

See [the cutover and rollback runbook](forgejo-stage1-cutover.md).

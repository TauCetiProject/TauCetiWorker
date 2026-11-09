# Human-operated Forgejo candidates

The Stage 1 operator accepts a reviewed Forgejo PR revision, builds its exact
integration candidate, requests GitHub Actions CI, and promotes that candidate
with an expected-old update of `main`. It uses the existing queue SQLite store,
Git candidate refs, request ledger, and CI evidence verifier. It does not run the
autonomous controller or require private Forgejo APIs.

This command is a preparation component. A successful candidate build does not
by itself establish migration readiness: the main mirror, post-merge build,
cache/docs publication, active-work transfer, backups, and cutover/rollback
rehearsal are separate Stage 1 requirements.

## Provision the protected inputs

Use a dedicated operator directory owned by the operator, mode `0700`. Keep its
configuration, profile, and credentials in private regular files, mode `0600`.
Use a dedicated empty queue database and trusted local Git clone. Never point
this command at the running controller's database. The two entry points refuse
each other's operating-mode marker; starting the controller on this database is
not a Stage 2 upgrade procedure.

The protected execution repository contains:

- `tauceti.toml`: schema v2 with explicit Forgejo content/roadmap identities,
  separate GitHub execution repository, configured Lean roots, and shared state
  disabled for this path.
- `.tauceti/ci-source.json`: the existing source-candidate `ExecutionPolicy`
  record, including native source/execution/harness repository IDs, trusted
  harness SHA, workflow path, branches, configuration digest, and publication
  requirement. Prepare it with the existing `tauceti_tools.ci_candidate` contract.
- A protected `.github/workflows/pr-build.yml` caller. The example in
  `TauCetiTools/examples/forgejo-stage1` includes `id-token: write`, required by
  its nested reusable workflow. Pin both reusable workflow and tooling to the
  same reviewed immutable Tools revision.
- For workflow dispatch mode, the protected `stage1-dispatch.yml` example from
  the same directory. It validates the request against the protected policy,
  retains an intent artifact before sending, and uses the repository's native
  `${{ github.token }}`. It never receives source write credentials.

Protect source `main` and `tauceti-ci/**`, including against administrator bypass,
so ordinary contributors cannot promote or overwrite retained candidates. Give
only the designated operator the necessary write/merge authority. Confirm this
with a contributor who actually has feature-branch write permission. Disable
other merge paths; do not use the web merge button to reconstruct a squash.

Set the execution repository's `LAKE_CACHE_ARTIFACT_ENDPOINT_PUBLIC` and
`LAKE_CACHE_REVISION_ENDPOINT_PUBLIC` variables to the exact public URLs in the
trusted configuration. A missing/mismatched variable refuses the source build.
When publication is required, also configure the isolated publisher's upload
endpoints and `LAKE_CACHE_KEY`. Never expose that secret to candidate build code.

Copy the exact trusted `tauceti.toml` bytes into the operator directory. Make the
profile below using the policy record from the protected execution repository;
replace the capitalized values and numeric IDs with observed values. Paths must
be absolute. Reviewer IDs are native IDs on this Forgejo origin.

```json
{
  "schema": "tauceti-stage1.operator/v1",
  "configuration": "/ABSOLUTE/PRIVATE/tauceti.toml",
  "execution_policy": "REPLACE WITH THE COMPLETE EXECUTION POLICY OBJECT",
  "workflow_revision": "FULL_40_HEX_PROTECTED_CALLER_SHA",
  "clone": "/ABSOLUTE/PRIVATE/operator-clone",
  "database": "/ABSOLUTE/PRIVATE/operator.sqlite",
  "source_token_file": "/ABSOLUTE/PRIVATE/source-token",
  "execution_credentials_dir": "/ABSOLUTE/PRIVATE/execution-app",
  "reviewer_ids": [123],
  "execution_mode": "workflow",
  "execution_read_token_file": "/ABSOLUTE/PRIVATE/execution-read-token"
}
```

The source token authenticates the allowlisted human on the configured Forgejo
origin and needs current source repository push permission. Human acceptance is
an explicit operator action, not an imported GitHub approval or PR-body claim.
The reviewer must perform the review before accepting the exact head.

In `workflow` mode, the explicit execution read credential is used only through
GET/evidence methods; this interface cannot dispatch or cancel. Provision a
credential restricted to the execution repository with Actions/contents read
permissions. The command cannot prove the credential has no additional privileges;
verify that when provisioning it. The `execution_credentials_dir` field is unused
in this mode. Do not give the source token to GitHub Actions.

Alternatively set `execution_mode` to `app` (the default), omit the read-token
field, and provision the existing `github-app.json` and `github-private-key.pem`
files under `execution_credentials_dir`. The installation must include this exact
execution repository. Tokens are minted with repository-ID restrictions and
`contents: write`/`actions: write`. There is no ambient `gh` or PAT dispatch fallback.

## Accept, test, and promote

Run these commands from a trusted operator environment using an installed Worker
revision and its pinned dependencies. Record those revisions with the rehearsal.
Here `$profile` is the absolute profile path; `$attempt` is the ID returned by
`prepare`. Shell variables below are local operator choices, not candidate inputs.

1. Inspect the native PR and its diff. Record the full head SHA you reviewed.
   Coordinate with the contributor so its head stays fixed during publication.
2. Record acceptance and freeze the integration request:

   ```sh
   python -m tauceti_queue.stage1 --profile "$profile" prepare \
     --pr "$number" --accept-head "$reviewed_head"
   ```

3. In App mode, send once:

   ```sh
   python -m tauceti_queue.stage1 --profile "$profile" dispatch --attempt "$attempt"
   ```

   In workflow mode, retain the candidate and export the handoff once:

   ```sh
   python -m tauceti_queue.stage1 --profile "$profile" handoff \
     --attempt "$attempt" --out "$private_directory/handoff.json"
   gh workflow run stage1-dispatch.yml --repo "$execution_repository" \
     --ref main --json < "$private_directory/handoff.json"
   ```

   This `gh` invocation uses the genuine GitHub CLI in the operator environment,
   explicitly targeting the execution repository. Record the resulting parent
   run's URL. Submit one parent per request; uploading the same file into a new
   parent is another dispatch, not recovery. The first verified child owns the
   request; a matching run title alone is insufficient.

4. Find the child under the execution repository's source-candidate workflow and
   adopt it only through receipt verification:

   ```sh
   python -m tauceti_queue.stage1 --profile "$profile" observe \
     --attempt "$attempt" --run-id "$child_run_id"
   ```

   Subsequent `observe --attempt "$attempt"` reads the selected owner's latest
   attempt. Run IDs and attempts are execution identities, not source PR numbers.
   Use one `gh run watch` monitor when waiting for the hosted run.
5. Require a verified passing result, then promote:

   ```sh
   python -m tauceti_queue.stage1 --profile "$profile" promote --attempt "$attempt"
   ```

   The command reads current CI evidence and source facts again, then pushes the
   exact candidate with an expected-old base. A changed head/base requires fresh
   acceptance and CI. It does not execute contributor code locally.
6. Inspect the native PR's current head once more. If it still matches the landed
   acceptance, comment with the candidate and CI URL and close it using the supported
   native `gh pr close` path. A squash leaves the original PR head outside main,
   so this cleanup is a named human responsibility. Preserve any new contributor
   revision for further review rather than closing it as though it was tested.
7. Verify mirror equality and the essential post-merge outputs separately. Record
   their exact commit IDs and public artifacts before declaring the rehearsal done.

Repository renames/deletion, reviewer-policy rotation, and concurrent head edits
must not overlap this manual publication window. Between commands, reviewer-list
changes are journaled; removing a reviewer revokes their pending acceptances.
A new reviewer must explicitly review and accept a new request. Before any request
exists, an unused ledger can be rebound to correct a configuration typo. Stock Git provides atomic
expected-old protection for main; PR head reads across a contributor fork and the
main push are separate observations. Use the coordinated human-operated window;
this command is not an autonomous cross-repository transaction.

## Failures and recovery

- Exit `77` means evidence is unavailable or still pending. A failed build refuses
  promotion with exit `78`; it is not an indefinite successful-build wait.
- An invalid/existing handoff output path fails before state changes. If the
  output was lost or writing was interrupted after the handoff was committed,
  recover the same frozen payload without sending anything:

  ```sh
  python -m tauceti_queue.stage1 --profile "$profile" export-handoff \
    --attempt "$attempt" --out "$private_directory/recovered-handoff.json"
  ```

  Export does not authorize another parent invocation. First settle the original
  dispatch. Do not clear `sending`/`handoff` or edit the request ledger to force a
  resend after a lost acknowledgement.
- The shipped parent template permits rerunning the *same parent* only when its
  read-only jobs evidence proves every earlier attempt skipped the send step.
  Missing/incomplete evidence, or a send step that failed, succeeded, or was
  cancelled, refuses transmission. Older deployed parent revisions may forbid
  every rerun; inspect the exact pinned workflow, not just this document.
- A known failed child may be rerun in place using GitHub Actions. Observation
  verifies its latest attempt; an old passing artifact cannot authorize a merge
  while a newer attempt is pending, failed, or unavailable. Repairing source code
  requires accepting the new head and preparing a new candidate instead.
- A lost dispatch acknowledgement with no verified child remains unresolved.
  Reconcile the original run and retained intent; absence from one run listing
  is not proof that an event was never accepted. No automatic retransmission is
  permitted. Leave candidate refs and the database intact. If the operator elects
  to replace an unresolved request, explicitly retire its authority first:

  ```sh
  python -m tauceti_queue.stage1 --profile "$profile" abandon \
    --attempt "$attempt" --reason "Original dispatch unresolved; explicitly replace it"
  ```

  Then review/prepare again to obtain a new request ID. A late old run cannot
  authorize promotion of the abandoned request. Existing runs are not cancelled
  by abandonment and may still consume CI time; settle/cancel known owned runs
  separately. This action never repeats paid authoring or review inference.
  Abandonment is refused if a promotion intent exists: reconcile the potentially
  completed source push first.
- After a lost promotion acknowledgement, run `promote` again. A recorded verified
  intent plus the candidate on main (or in main's ancestry) reconciles read-only.
  It does not repeat the push. A candidate found on main without such an intent
  requires investigation. Completed candidates are recorded terminal, and empty
  or already-landed PRs are refused before another request is allocated.
- Retain the database, protected configuration, credential material, Git refs and
  clone in the recovery package. Protect credential backups. Do not delete them
  to reset uncertain work. Change reviewer policies between publication commands;
  a source/execution role change after requests exist requires a separately
  prepared ledger while retaining and settling the old one.

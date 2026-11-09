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
  "mirror_main": false,
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
for CI dispatch in this mode; the optional main mirror uses it for its separate
App role. Do not give the source token to GitHub Actions.

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

- Exit `75` means another operator/service owns the publication lock; retry later.
  Exit `77` means evidence is unavailable or still pending. A failed build refuses
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

## Automatic main mirror and repair

Enable `mirror_main: true` only after provisioning the GitHub App installation
for the exact execution repository and preparing its main-branch permissions.
The App must have `contents: write` and `workflows: write`; each mirror invocation
mints a token restricted to that native repository ID and those two permissions.
Workflow-file permission is necessary because mirrored main can contain trusted
workflow updates. The mirror does not use a personal token or native job token:
an App's Git push triggers the intended GitHub push workflows.

The profile's `execution_credentials_dir` supplies the existing App metadata/key
files for this role even when candidate CI uses workflow mode. The mirror keeps
source and GitHub credentials in separate process environments and does not
persist GitHub tokens in clone configuration, command arguments, or the ledger.
Only `refs/heads/main` is pushed, with tag following, submodule recursion and hooks
explicitly disabled. Tags and candidate/member branches are not part
of this automatic publication path.

An enabled profile automatically mirrors a recorded, verified Stage 1 promotion
after `promote`,
including when that command reconciles a lost source-push acknowledgement. The
source promotion is recorded complete before the mirror is attempted. If the
mirror fails, the command explicitly reports that the source is already landed
and the mirror needs recovery; it does not roll back or merge the source again.

Repair or confirm the mirror directly:

```sh
python -m tauceti_queue.stage1 --profile "$profile" mirror
```

The command reads native source identity/current main, fetches that exact source
commit into the trusted clone, validates GitHub's repository ID/default branch,
and permits only a fast-forward from the observed GitHub main. The push carries
an explicit expected-old lease. A changed source tip or divergent/ahead GitHub
main is refused. Preserve both histories and settle authority rather than force
rewinding the mirror. A lost mirror response is reconciled by reading main again;
an already-equal mirror performs no push and triggers no duplicate build.

For crash recovery between source promotion and mirroring, schedule this same
`mirror` command on the trusted operator host. The existing per-ledger service
lock prevents it from overlapping another operator publication. Activating that
schedule and transferring live merge authority belong to the separately authorized
cutover. Disable old GitHub source-writing automations and grant only the intended
mirror App the required main update permission during that authority transfer.
Verify actual push-triggered builds, publication outputs, and equality of both
main refs in the isolated rehearsal before enabling the production profile.


With mirroring enabled, `workflow_revision` in the private profile is the approved
initial control-file commit. Each later source base can become the next exact
CI caller only if the ledger proves its completed Stage 1 promotion and its
`.github/`, `.tauceti/`, `tauceti.toml`, and `scripts/` bytes still match that
approved commit. Each new request records the current base/caller SHA. This permits
the second and later contributions without editing the profile after every merge.
An out-of-band main update has no such record and cannot be mirrored or adopted
as the next caller. Workflow/control changes need a separately reviewed deployment
and explicit new profile pin; a normal contribution cannot silently replace the
trusted caller.

The mirror preflight requires GitHub main to equal the captured source base.
Repair any prior lag before dispatching/promoting another contribution. If the
mirror fails after source promotion, a definite refusal is exit `78` with the
source-landed message; incomplete/transport evidence is exit `77` with that same
source-landed distinction. Unexpected failures are reported without leaking the
credential response. Mirror intent and pending/confirmed result state are recorded
in the existing ledger; a failed attempt does not leave an old success labelled
as the current result. Git timeouts terminate the owned process group before
recovery, and ref reads use the native GitHub repository-ID route.

The initial approved main, and a newly repinned main after a reviewed deployment,
can be confirmed read-only when source and GitHub already match. That bootstrap
case never pushes: a mismatch must be settled by the reviewed deployment. Rebuilt
operator clones fetch missing source/pin objects before comparing execution
controls. Completed promotion records remain historical facts under their original
policy; a harness/configuration upgrade does not relabel them or prevent repair
of their already-landed main. Preparing new work requires the profile's exact
configuration/policy bytes to match the newly approved caller.

For a control or policy upgrade, first settle current sends, promotions and mirror
lag. Prepare and test the exact new configuration/workflow commit in an isolated
rehearsal, then obtain the separate deployment authorization. Install that reviewed
control commit on both protected mains using their expected old revisions, update
the private configuration/policy and `workflow_revision` pin, and confirm equality
with `mirror` before admitting new contributions. Retain the old ledger and
configuration for recovery. This is a trusted configuration deployment outside
`prepare`/`promote`; those commands refuse control-file changes before dispatching
CI and never waive the ordinary source-candidate scope/bump gates.

The CI and mirror permission subsets can come from the same App installation;
they are not different GitHub App identities. App-level branch exceptions apply
to both in App CI mode. Workflow CI mode uses GitHub's native job token, which can
be excluded from the mirror App's main exception. For an unattended mirror, a
dedicated read-only Forgejo account should be listed in the optional
`mirror_actor_ids` profile field. Specifying this nonempty field makes the profile
mirror-only, and its native IDs must be distinct from `reviewer_ids`. The profile
can confirm/repair the mirror but cannot prepare
acceptance, hand off or dispatch CI, abandon a request, or promote source work,
even if its repository metadata reports push permission or its token was
accidentally replaced by a human reviewer's token. Use a human profile without
`mirror_actor_ids` for acceptance/promotion; it can still mirror automatically.

Give the dedicated account no repository write role and mint a read-scoped token.
Repository permission metadata can describe account-level permissions rather than
the token's scope; a human reviewer's read-scoped token is not a separate mirror
identity. Create the human ledger first. Mirror-only sessions check their actors
against its authoritative current reviewer list and refuse overlap. They never
rewrite that list or journal reviewer rotation from a stale scheduled profile.
Keep the profile's `reviewer_ids` current for clarity; human sessions alone rotate
acceptance authority.
For example, with native human reviewer `123` and dedicated mirror reader `456`,
both profiles have `"reviewer_ids": [123]`; the scheduled profile additionally has
`"mirror_actor_ids": [456]` and uses reader `456`'s token file.

Profiles, ledger, clone, and App files remain trusted operator-host state. These
command roles do not isolate untrusted code running as the same OS user. Candidate
execution must never receive that host state or either source token.

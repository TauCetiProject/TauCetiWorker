# Run one merge queue with GitHub and Forgejo inputs

The queue defaults to GitHub only. `forgejo_mode = "disabled"` does not load the
Forgejo profile, credentials or client, and makes no native API or Git requests.
`publication_enabled` remains an independent control. Installing this code does
not activate native discovery, admission, publication, mirroring or a primary
change.

Set these **top-level** service configuration keys before `[settings]`:

```toml
forgejo_mode = "disabled" # disabled | observe | admit
forgejo_profile = ""      # absolute private JSON path, used only when enabled
forgejo_admission_enabled = true # initial runtime setting; effective only in admit mode
```

The equivalent environment variables are `TAUCETI_QUEUE_FORGEJO_MODE` and
`TAUCETI_QUEUE_FORGEJO_PROFILE`. Source enrollment is startup configuration, not
a mutable worker request or PR-controlled setting. Enabled modes require the
existing `admission_source = "policy"`. Keep using the existing queue database,
clone, GitHub App credentials, claims, retained refs and publication outbox.
Run a single writable service for that database; all service modes acquire the
same private process lock.

`observe` runs a separate direct native admission poller and records its decisions
in the existing admission ledger. It does not approve native inputs or mutate
Forgejo. GitHub admissions, worker claims, builds and landings continue through
the existing queue. Native discovery errors appear in status without stopping
GitHub discovery. The poller lists every open Forgejo PR at most once per
`approval_poll_interval_seconds` (or at once when discovery is requested, for
example by re-enabling Forgejo admission), and spends the passes in between,
each bounded by `reconcile_interval_seconds`, on evaluations. Each of those
passes first lists, newest first, only the open PRs Forgejo updated since the
newest update time already seen (less two minutes, and less the length of the
last full listing), so a new PR, push, label or comment is seen within a pass;
a recent listing that needs more than four pages of 50 asks for a full listing
instead. Editing a comment does not change a Forgejo PR's update time (the review bot
edits its scoreboard in place), so each of those passes also reads the
repository's comment feed for comments created or edited since its last read
(the position is kept across restarts, and moves only after a complete read)
and evaluates again any settled PR with a new scoreboard or recovery request or
an edited comment. Evidence neither shows is rechecked
`forgejo_recheck_seconds` (default 6 hours) after a PR's last evaluation. The
runtime setting `forgejo_recent_listing` (default true) turns the recent
listings and the comment feed off.
Because mixed
landing freezes a contribution at admission, an approved Forgejo input is read
once more just before it joins a batch: unless the PR still shows the listed
facts admission evaluated (head, state, labels, body, update time, comment
count) and the merge base it was reviewed against, and unless its review, read
and judged once more, still admits the same scoreboard, the insertion is held
and the PR is evaluated again, and a blocking
label (`hold`, `wip` and the others approval refuses) holds it while it stays.
`admit` enables native inputs in that same queue, including
qualified repair tasks and exact candidate CI. Pause new native acceptance with
the authenticated runtime setting, keeping `forgejo_mode = "admit"`:

```sh
tauceti-queue settings set forgejo_admission_enabled false
# Resume with a fresh evaluation of currently open native heads:
tauceti-queue settings set forgejo_admission_enabled true
```

The setting persists across restart. Accepted waiting/reserved inputs, existing
batches, paid repair claims, CI, source publication and main synchronization
continue while new native acceptance is paused. GitHub intake continues. A
concurrent native poll cannot admit new work after the pause takes effect; a
resume invalidates observations for unaccepted work and reevaluates it.
`publication_enabled = false`
continues to prevent queue publication and CI dispatch.

An explicit private profile uses this shape. IDs, paths and revisions below are
examples, not deployment values. Profile and credential files must be owned,
private regular files as required by the existing control-policy loader.

```json
{
  "schema": "tauceti-queue.mixed-sources/v1",
  "github": {"forge": {"kind": "github", "url": "https://github.com"}, "name": "team/Math", "id": 101},
  "forgejo": {"forge": {"kind": "forgejo", "url": "https://git.example.org"}, "name": "team/Math", "id": 41},
  "publisher_id": 4,
  "source_token_file": "/home/operator/.config/tauceti-queue/native-source-token",
  "execution": {
    "configuration_file": "/home/operator/.config/tauceti-queue/ci-source.toml",
    "policy_file": "/home/operator/.config/tauceti-queue/ci-source.json",
    "credentials_dir": "/home/operator/.config/tauceti-queue/execution"
  },
  "mirror": {
    "forgejo_token_file": "/home/operator/.config/tauceti-queue/native-main-token",
    "forgejo_writer_id": 6,
    "github_credentials_dir": "/home/operator/.config/tauceti-queue/github-mirror"
  }
}
```

Observation needs only the schema, source identities, publisher ID and
source token path. Older profiles' `reviewer_ids` and `authority_generation`
keys are accepted and ignored. Admission additionally requires execution and mirror roles.
`check-config` validates the profile shape; it does not prove enabled-mode
readiness. Startup also validates policy/configuration bytes, stored source
bindings, credentials and remote repository identities.
Execution-main selection happens when preparing a new CI request, so an
execution outage or invalid new caller does not prevent the service from
starting and serving existing work. Request preparation reports and retries
the execution error through the existing CI freeze-error path.
Validate an enabled configuration against a disposable queue before applying it
to the live service. A configuration check alone is insufficient for rollout.
The source account needs repository access sufficient to read the
repository permissions of commit-status creators. In admission mode its authenticated native ID must match
`publisher_id` before source publication or retention. Protect the native CI
retention namespace for that actor. The protected-main writer must use a
separate actor and token. The GitHub execution App role is repository-ID-scoped
with `contents:write, actions:write`; the follower mirror uses its separate
`contents:write, workflows:write` role. No role is inferred from `gh` login or a
similarly named account on the other host.

Native review admission is the same as GitHub's. The shared review policy
counts the newest completed scoreboard for the current head and merge base,
whoever posted it; the hard boundary is CI, scope, the axiom audit and
bump-guard. Reviewers publish with the common Review poster, as on GitHub, and
older boards carrying a `tauceti-source:v2` provenance block still count. Only
recovery holds are author-bound: a `Merge-queue recovery for head` comment
counts from `publisher_id`, as GitHub counts it from the queue's bot logins.

Forgejo, like GitHub, accepts `POST /repos/{owner}/{repo}/statuses/{sha}` only
from users with write access to the repository's code. Forgejo Actions also
posts statuses, as its system user, for any workflow a PR brings, under a
context the workflow names. A native `build`, `scope`, `bump-guard` or
`self-tested/*` status therefore counts only if its creator currently has
write, admin or owner permission. Mixed admission ignores native statuses
altogether and requires exact candidate CI.

Credential-path, caller and mirror changes preserve paid repair claims, which
bind to the enrolled repositories, queue epoch and CI scope. Landing always
rechecks the current review verdict.

The execution policy must enroll both exact repository identities in its
`member_sources` array. Its `source` is the Forgejo **retention** repository;
`execution` is the independently configured GitHub Actions repository. They need
not match the original source of any member. Deploy the pinned shared harness,
`.tauceti/ci-source.json` and matching `.tauceti/ci-source.toml` to the trusted
execution caller before enabling admission. Ordinary GitHub CI keeps using the
project's `tauceti.toml`. The private policy and configuration enroll the source,
execution and harness repositories and credential origin. New requests read the
current policy and configuration from protected execution `main`; normal tooling
and configuration updates there do not require replacing the private bootstrap
files. Changing enrolled repository identities or credential roles remains an
explicit configuration change. Legacy GitHub attempts already running retain
their original CI path. See the shared Tools source-candidate contract for the complete
workflow and token setup.

A mixed batch freezes every original source, native repository ID, PR number and
accepted head. Same-numbered PRs have distinct membership, refs, provenance,
claims and publication destinations. Native provenance includes the repository
ID, for example `forgejo:https://git.example.org/team/Math@41#7`. Repair refs and
repair-worker authentication continue using the GitHub queue repository. The
mixed task schema preserves member identities through worker checkpoints and
refuses missing or stale authority bindings. No native source credential is sent
to the repair worker. New runners advertise the mixed task schema when claiming;
older runners receive legacy work only and cannot park a mixed claim until expiry.
Deploy the matching repair runner before enabling mixed admission. CI validates exact retained bytes, full mixed lint and the
existing per-member scope/dependency gates; landing rechecks original heads,
targets, holds and current review authority.

Human-approved GitHub control-file changes keep their existing GitHub-only CI
path. Source-candidate CI refuses any batch whose diff leaves the native path
policy (paths under the policy's prefix plus its allow list), so such a PR can
never share a batch with a Forgejo PR. Under Forgejo primary every batch goes
through source-candidate CI, and these changes wait with an explicit reason
(`require GitHub primary and a GitHub-only batch`); switch temporarily to GitHub
primary to land them. A primary change to Forgejo refuses pending batches
containing these changes.

### The control-file lane

Without help, a control-file PR blocks Forgejo PRs in both directions: probed
into a batch with Forgejo members it is refused, and once it is in a GitHub-only
batch every Forgejo PR probed there is refused (`native candidate needs a
compatible batch or changes within the native path policy`) until that batch
lands. The `control_lane_enabled` setting (off by default) gives these PRs a lane
of their own. It applies only while mixed sources are admitted under GitHub
primary; otherwise every rule below is off.

- **Eligibility.** Each waiting GitHub input is classified once, before routing:
  it is a control-file change when its own diff, from its merge base with `main`
  to its accepted head, leaves the native path policy (exactly what
  source-candidate CI refuses). This covers `.github/`, `scripts/`, lakefiles,
  docs and any other path outside the policy. A PR that also touches native paths
  goes to the lane whole. The classification is stored in the input's
  `evidence.control_files`; a push makes a new input, which is classified again.
  Forgejo PRs are never lane members: one outside the policy keeps today's wait.
- **The lane batch.** At most one open lane batch (`batches.lane = 'control'`)
  exists. It is created on `main` when a control-file input waits, takes every
  waiting control-file input first in first out, and is removed when it is empty
  and stale or nothing waits for it. Ordinary insertion never probes a
  control-file input, so ordinary batches never carry paths outside the policy.
  The lane batch does not count against `open_batch_limit` and gets no
  preparation, pre-rebase or successor sealing.
- **Priority.** When the head lands, the next head is the emergency batch if any,
  then the lane batch, then the oldest ordinary open batch. The lane never evicts
  a running head (status reason: `control-file lane batch N waits for head M to
  land`) and has no fairness guard: lane heads may run back to back. Claimed
  `pre_rebase` and pre-rebased `prepare` work on the ordinary successor survives
  a lane head as it does an emergency head, and a pre-rebase made on the previous
  head's tip is carried onto `main` by replay when the successor seals.
- **CI.** A lane head has only GitHub members, so it is dispatched through
  GitHub-native `pr-build.yml`, whose `scope` status checks the admin approval
  for every restricted path. A lane head that changes `lakefile.*` or Lean files
  under `scripts/` changes Lake build inputs: it gets no cache hit and the next
  head waits for its base cache to be published.
- **No repair.** A control-file input gets `no_repair`, which no label event
  clears. Repair agents never edit it: a failing lane head goes to repair like
  any other, and a repair may only empty a member, which the queue then
  withdraws as `problem-anticipated`. The
  withdrawal comment says the queue does not edit control files.

Status shows `lane` on every open batch and on the head, `control` on each
waiting input, and the number of waiting control-file inputs in
`waiting.control`. The journal records `control-classified`,
`control-lane-opened` and `control-lane-head`.

**Enabling.** Deploy with the setting off, then run
`uv run tauceti-queue settings set control_lane_enabled true` (or post
`{"control_lane_enabled": true}` to `/admin/settings`). The next tick classifies
the waiting GitHub inputs and opens the lane batch.

**Disabling.** Set it to `false`. A live lane batch is then staged as an ordinary
open batch, oldest first, and nothing else needs cleaning up. Inputs already
classified keep `no_repair`.

**A batch that already carries a control-file PR.** Enabling the lane does not
move members inserted before it: membership of a batch is fixed, and the queue
has no command that removes a member from an open batch. Such a batch keeps
refusing Forgejo PRs until it seals, and it drains naturally when it lands. The
Forgejo PRs it refused are not lost: their conflict marker names only that
batch's base, so they enter the next ordinary open batch as soon as one exists.
To confirm that a batch is in this state, list its paths outside the policy in
the queue clone on the host: `git diff --name-only <base> <tip>`, filtered by the
policy prefix and its allow list. A `grep -v '^TauCeti/'` alone also flags
allowed files such as `TauCeti.lean`, `lake-manifest.json`, `lean-toolchain` and
`docbuild/docs/references.bib`.

Each new mixed/source-candidate request captures the configured execution
repository's protected `main` SHA, `.tauceti/ci-source.json`, matching
`.tauceti/ci-source.toml` and the configured caller at that exact commit. The
caller runs the shared harness from TauCetiTools `main`. Source, execution, harness,
credential origin and enrolled member roles must match enrollment; a candidate
branch cannot select a caller or grant credentials. The execution repository
can be separate from the GitHub content repository.

The queue does not read the caller's harness literals and asks no broker before
dispatch. The restricted runner groups admit exactly
`TauCetiProject/TauCetiTools/.github/workflows/<file>@refs/heads/main`, and the
queue accepts a result only from a run whose referenced `pr-build.yml` is that
harness at `refs/heads/main` (TauCetiTools `ci_candidate`).

Ordinary reviewed changes to `.github`, `.tauceti`, `scripts` and `tauceti.toml`
on protected execution `main` are available to the next request without a
private caller approval or service restart. Existing frozen requests retain their
original caller, policy and configuration across retries and process restarts.
The run's caller, harness, configuration, candidate and attestation must match
that original request. A caller that moves before dispatch is a proven non-send;
the existing retry path prepares a fresh request. A lost dispatch reply is
resent with a fresh request once a complete run listing still shows no run for it
after `dispatch_timeout_seconds`.

Forgejo-to-GitHub follower publication checks controls against the captured
GitHub content `main` base, with the existing ancestry and expected-old checks.
It has no separate manually approved caller pin. Scope checks still prevent a
native candidate from changing trusted execution controls.

GitHub starts as primary. Landings enqueue follower synchronization in the
existing retryable outbox. Both primary writers use an atomic expected-old Git
lease and verify ancestry locally. A lost landing reply is reconciled by reading main;
a lost mirror reply is reconciled by reading both tips. Sync refuses divergence
and only fast-forwards. Queue status reports primary, generation, native
observation errors and the last confirmed sync. The old Bors-compatible feed
contains only GitHub members; it must never attribute a native PR to a GitHub
number. The main queue status and mixed repair schema expose qualified members.

The authenticated GitHub-admin operation below changes primary and the queue's
sync direction together. Use the existing service HTTP authentication mechanism:

```text
POST /admin/primary
{"primary":"forgejo"}
```

Use `{"primary":"github"}` to reverse it. This operation serializes with queue
landing, reconciles due outbox work, refuses uncertain pending landings and
requires identical protected `main` tips. It increments a durable generation so
old promotion intents cannot write in the new direction. Open PRs, queue entries,
claims and running CI remain in place. Reuse still requires the original frozen
candidate and applicable authority. Native primary requires a working GitHub
follower preflight. Disable the earlier standalone GitHub-to-Forgejo timer when
the queue takes ownership of synchronization. Do not run independent merge
queues or permit direct main writes on either host.

Use `forgejo_admission_enabled = false` to pause intake. Completely disabling
source access with `forgejo_mode = "disabled"` is for an unused or decommissioned
integration, not an operational intake pause. Without native access, retained
native batches wait for source recovery; a native-primary ledger requires its
active mixed configuration. Production enablement, hosted mixed CI/landing
evidence and production primary changes remain separate rollout actions.

### Landing GitHub work while Forgejo is unavailable

Under GitHub primary, a batch with no Forgejo member and no earlier
source-candidate attempt dispatches and lands through GitHub alone; a batch with
a Forgejo member needs Forgejo for both. When Forgejo cannot be used, hold
Forgejo inputs out of insertion, then break apart the batches that hold them:

```sh
tauceti-queue settings set forgejo_placement_enabled false
# POST /admin/evict-native (repository admin), body {} or {"withdraw": [<GitHub PR>, ...]}
```

`/admin/evict-native` removes every live batch (head and open) with an active
Forgejo member or a named GitHub PR; all their members wait again at their
original age with their approvals, and their repair edits are discarded. GitHub
inputs are inserted into new GitHub-only batches; batches already GitHub-only
are kept. A named PR is cancelled as `bors r-` would. It is refused while
`forgejo_placement_enabled` is true. Once Forgejo works again, set it to `true`
and the waiting Forgejo inputs are inserted as usual.

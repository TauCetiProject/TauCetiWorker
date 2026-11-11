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
GitHub discovery. `admit` enables native inputs in that same queue, including
qualified repair tasks and exact candidate CI. `publication_enabled = false`
continues to prevent queue publication and CI dispatch.

An explicit private profile uses this shape. IDs, paths and revisions below are
examples, not deployment values. Profile and credential files must be owned,
private regular files as required by the existing control-policy loader.

```json
{
  "schema": "tauceti-queue.mixed-sources/v1",
  "github": {"forge": {"kind": "github", "url": "https://github.com"}, "name": "team/Math", "id": 101},
  "forgejo": {"forge": {"kind": "forgejo", "url": "https://git.example.org"}, "name": "team/Math", "id": 41},
  "authority_generation": "11111111111111111111111111111111",
  "reviewer_ids": [3],
  "publisher_id": 4,
  "source_token_file": "/home/operator/.config/tauceti-queue/native-source-token",
  "execution": {
    "configuration_file": "/home/operator/.config/tauceti-queue/ci-source.toml",
    "policy_file": "/home/operator/.config/tauceti-queue/ci-source.json",
    "workflow_revision": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
    "credentials_dir": "/home/operator/.config/tauceti-queue/execution"
  },
  "mirror": {
    "forgejo_token_file": "/home/operator/.config/tauceti-queue/native-main-token",
    "forgejo_writer_id": 6,
    "github_credentials_dir": "/home/operator/.config/tauceti-queue/github-mirror",
    "approved_caller": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
  }
}
```

Observation needs only the schema, source identities, reviewer/publisher IDs and
source token path. Admission additionally requires execution and mirror roles.
The source account needs repository access sufficient for current native
publisher ACL checks. In admission mode its authenticated native ID must match
`publisher_id` before source publication or retention. Protect the native CI
retention namespace for that actor. The protected-main writer must use a
separate actor and token. The GitHub execution App role is repository-ID-scoped
with `contents:write, actions:write`; the follower mirror uses its separate
`contents:write, workflows:write` role. No role is inferred from `gh` login or a
similarly named account on the other host.

Native review admission uses the existing revision-bound native publication
protocol, including immutable status receipts, current publisher ACLs, body
hashes and recovery holds. Ordinary scoreboards alone do not authorize admission:
Forgejo writers can edit another author's comment. For controlled rollout,
publish native approval using the existing `ForgejoSourceAdapter.prepare_publication`
and `publish` operations with this profile's publisher policy and a durable
sequence. Stage 1B Worker comments remain useful discussion; completing automatic
trusted Review publication is Stage 2 work. Never bypass this gate to make a
canary enter the queue. The explicit `authority_generation` is a fresh nonzero 128-bit value; rotate it
when changing native review authority and never reuse a retired generation.
Credential-path, caller and mirror changes preserve reviews and paid repair
claims. A trust change can require fresh publication of the retained review;
it does not require paying for the review again.

The execution policy must enroll both exact repository identities in its
`member_sources` array. Its `source` is the Forgejo **retention** repository;
`execution` is the independently configured GitHub Actions repository. They need
not match the original source of any member. Deploy the pinned shared harness,
`.tauceti/ci-source.json` and matching `.tauceti/ci-source.toml` to the trusted
execution caller before enabling admission. Ordinary GitHub CI keeps using the
project's `tauceti.toml`. The private configuration bytes must exactly match the
trusted source configuration. Legacy GitHub attempts already running retain their
original CI path. See the shared Tools source-candidate contract for the complete
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

When the execution repository is also the moving GitHub content repository,
new requests capture its current caller SHA only after comparing execution
control files with the explicitly approved caller. In-flight requests retain
their original immutable caller. Changes to `.github`, `.tauceti`, `scripts` or
`tauceti.toml` require an explicit approval/configuration update.

GitHub starts as primary. Landings enqueue follower synchronization in the
existing retryable outbox. A lost landing reply is reconciled by reading main;
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

Disabling native admission retains mixed work and pending native publications;
it does not reinterpret them as GitHub work. A native-primary ledger refuses to
start without its explicit active mixed configuration. Switch back with matching
tips before disabling the native primary's configuration. Production enablement,
hosted mixed CI/landing evidence and production primary changes are separate
rollout actions.

# Concurrent local verification and exact-input receipts

`tauceti verify [--stage build|axioms|module-system|lint|shim-expiry|all]
[--base SHA] [--json] [--no-reuse] [--lint-modules FILE]` runs synchronously in a TauCeti checkout. It exits zero
only after every requested phase succeeds against unchanged inputs. Progress, original tool output
and actual exits go to stderr; `--json` writes one `tauceti-verification/v1` object on stdout.
V0 authoring/repair dispatch remains the default. `TAUCETI_VERIFY_HELPER=1` opts a selected
natural canary into helper prompts; the helper CLI and receipt support are available separately.
Helper prompts invoke the installed absolute `scripts/tauceti-verify` wrapper with the worker's Python
interpreter. The wrapper also honors `TAUCETI_VERIFY_PYTHON` and supports Bubble's staged layout.
Wheel entry points restore its execute permission, so login-shell PATH changes cannot hide it. Existing `tauceti work`, module and checkout entry
points keep working.

The worker pins `TAUCETI_VERIFY_BASE` from the existing canonical-main checkout preparation,
`TAUCETI_VERIFY_POLICY` from surveyed labels/branch, `TAUCETI_VERIFY_STATE` to worker state, and
`TAUCETI_ROUND_DEADLINE` to the round's deadline. Standalone invocations resolve canonical main
from local Git refs only. No scope or verification step requests GitHub data or fetches refs.
The Python scope contract in `verification_scope.py` is tested against #305's V0 recipe: merge-base
diff, both rename ends, staged/dirty/deleted/untracked sources, ignored sources under TauCeti,
full CI requirements for broad policy/pins/deletions/unrepresentable modules, and unknown scope on missing
history. Missing canonical main fails closed with an incomplete receipt: there is no resolved base to
bind evidence to. Unknown lint scope remains incomplete instead of silently establishing lint evidence.
A stage-only build or audit does not claim the skipped requested lint succeeded.

All stages run the whole-library `lake build --iofail`, fast changed-file axiom diagnostics,
module-system, source style lint, changed-module environment lint, and the shim ratchet.
A stage-only call runs that stage; it does not assert that prerequisites or other stages passed.
The shim checker receives merge-base source and registry snapshots, strict `--fail-on-available`,
and `--only-new` for unchanged pins outside bump policy. Older source revisions without the checker
record an empty shim phase. CI still owns whole-library environment lint. For failures in unchanged
modules, `--lint-modules /absolute/list` adds validated existing dotted TauCeti module names to the
local lint list; its contents are fingerprinted. Use the same option for final verification. Write this
list and any extra diagnostic logs outside the checkout; untracked checkout files are inputs.

Local axiom checks use the bundled `scripts/LocalAxioms.lean`, through the absolute
`scripts/tauceti-local-axioms --modules FILE` wrapper. They read only the selected modules' own
`.olean`, `.olean.server` and `.olean.private` data, without loading a Lean environment,
following imports, reading Mathlib artifacts or running candidate initializers. They reject axiom
declarations and direct references to `sorryAx`, `Lean.ofReduceBool`, `Lean.ofReduceNat` and
`Lean.trustCompiler`, including private/opaque proof bodies. Missing/unreadable artifacts fail, an
empty docs/deletion-only list is valid, and unknown scope fails instead of loading the library.
Even bump/full-lint/deletion scopes check only their listed surviving changed modules locally.
These are fast diagnostics after a kernel-checked build, not a transitive axiom or global
declaration-ownership certificate. Those checks, including downstream importers and changed
dependencies, remain in authoritative CI. `lake exe axioms` is not used locally: its filter only
narrows traversal after a full environment import. Policy version 4 and the bundled driver/wrapper
hashes invalidate earlier local verification receipts. The wrapper compiles a small native executable
once per checker/toolchain identity, under a flock and atomic publication, then executes it directly.
There is no Lake or Lean interpreter startup on the warm path, no result caching, and no interpreter
or full-library fallback. Host binaries live in an owned private `~/.cache/tauceti/local-axioms`
directory in the login user's home (`TAUCETI_LOCAL_AXIOMS_CACHE` overrides it, isolated HOME does not);
Bubble uses its own persistent per-worker
cache, never the host's executable cache. Interrupted compilation leaves no published executable and
retries automatically. Four recent native versions are retained. Empty scopes need no toolchain.
Local axiom and module-system phases always rerun against the current artifacts, because a successful
stage-only check before a build cannot certify the outputs of that later build. Compilation/tool
failures are incomplete verification, distinct from a found axiom violation.

Each invocation atomically writes `state/<worker>/verification/<id>/receipt.json` before launching
anything and updates it through completion. File and directory fsync precede publication. The receipt
records requested phases, scope, start/end timestamps, tool exits, best-effort cleanup
and full log paths. Reused phases refer to their original receipt.
Receipts and logs are retained on failure or interruption. SIGKILL leaves the last durable receipt
incomplete, even if a tool appeared to finish. No background completion is assumed.

Evidence is reusable by phase only after that phase succeeds and its before/after inputs validate
against the same digest. Each newly completed phase is indexed before continuing: an interruption
in a later phase preserves earlier evidence without claiming the entire invocation succeeded.
The digest covers the full Git source inventory and actual bytes (including missing, dirty, renamed,
untracked and ignored TauCeti sources), base SHA, dependency sources, configuration, environment
value hashes, installed Lean distribution, executable hashes and versions, Python runtime, and worker
verifier code/policy. Sensitive environment values are not written. Changes conservatively invalidate
all phases. Complete inventories are stored once under `verification-inputs/<digest>.json`, referenced
from receipts. A phase index avoids scanning old receipts. Per-file digests are cached by path,
device, inode, size, mtime and ctime; any metadata change rereads the bytes. The metadata cache is
bounded. Receipts and logs remain retained; there is no automatic deletion of incomplete evidence.
After an immutable identity validates, inventory-only hashes for it are omitted from the persisted
`verification-hashes-v2.json` cache. Individually fingerprinted executable hashes remain cached even
inside that installation. Directory paths are stored once; mutable input records remain bounded at
200,000. Persisted directory recency and priority for currently used keys keep active hashes ahead of cold history.
Cache saves merge only touched keys under a bounded one-second per-state lock; an unavailable cache
cannot change the verification result or skip runtime cleanup.
Migration imports matching V1 stat-keyed hashes without rereading their payloads until each workspace
completes preparation. Early deadlines and isolated continuations retain migration eligibility for
the original workspace. Completion markers live separately in `verification-hash-roots/`, with one exact lookup per
workspace and no history scan. Warm preparation in migrated workspaces reads only V2, including
after source changes. Fresh isolated candidates skip opaque V1 history with nonmatching paths/inodes.
Repeated continuations reuse their V2 hashes. A toolchain switch may require one new payload hash
if its stat key is absent from V2, even when an old V1 file has that key. The V1 file remains intact for
rollback and may be removed once rollback support is no longer needed. Immutable identity files and
receipt indexes keep their existing formats.
The warm bound assumes a matching validated installation identity. A new identity after a reboot,
mount/provenance change or identity-cache cleanup may require a full cold payload hash under the
installation lock; other callers reuse the resulting seal. Writable/unavailable-cache fallbacks
retain ordinary stat-keyed hashes and complete input validation.
Temporary shim archives are removed after use. Source metadata sampling every two seconds catches mutations restored to their original bytes; a final full digest
check rejects changed inputs. Mutable toolchain/dependency metadata is checked at phase boundaries rather
than repeatedly scanning large installations during tool execution. Environment hashing is limited to build
configuration and tool-search variables; provider credentials and agent-session variables are excluded. Generated Lake outputs are not source inputs. These fingerprints identify
built inputs; they do not establish proof correctness or make writable caches or local receipts a
security boundary. Verification never weakens the axiom checks.

Independent workers run build, axiom/duplicate audit, module-system audit and lint commands
concurrently. Verification adds no host-wide slot limit, ticket scheduler, polling wait or
capacity-specific retry/accounting path. Existing worker/fleet admission and host CPU/memory
limits continue to apply.
Per-job leases prevent duplicate execution of the same candidate; they never serialize independent
candidates. Source style still runs before the default build so inexpensive diagnostics arrive early.

`TAUCETI_VERIFY_CACHE_DIR` selects the shared immutable-installation identity cache. The default
uses the host boot/machine identity and UID under `/tmp`. The directory must be owned by the user
and not writable by others. Bubble mounts it at `/opt/verification-cache` and persists receipts
separately under worker state. Cache locks protect creation/validation of one installation identity;
they are released before expensive verification commands run. Unregistered writable installations
retain complete inventory validation without requiring a shared cache. If the shared cache is
unavailable or unsafe, host verification falls back to complete validation and Bubble uses a
private cache directory. Cache availability never grants success or prevents the checks from running.

`TAUCETI_VERIFY_CHECKPOINT_MARGIN` reserves 600 seconds before the round deadline by default (a
nonnegative number of seconds; fixtures explicitly use zero). At that boundary the helper returns
incomplete with “checkpoint now”. Bubble candidate edits are saved as a base-bound binary patch and
an untracked-file archive in `verification-checkpoints/` before checks begin, including when there is
insufficient time to start verification. These artifacts are retained alongside a durable verification job in `verification-pending/`.
Interrupted candidates are copied to independent `verification-candidates/` workspaces; disposable Bubble candidates are
preserved before a non-reused phase starts. A fully reused invocation copies no dependencies or
Git history. Copies preserve actual sources/index, local Git history and warm Lake/Mathlib build
caches. The original Git index is read through a private copy. Linux uses independent CoW extents
when supported, with portable copying otherwise; trusted external immutable host dependencies can
retain their validated read-only identity. Absolute/escaping source symlinks fail incomplete because
they need specific recovery ownership. Copying still has production storage/I/O cost to measure
before broad dispatch. No clone contacts a remote. Candidate source/checkpoint data is retained for
publication/reconciliation; disk retention and package history cost remain canary measurements.

Interrupted CLI results remain `status: incomplete`, exit nonzero, and include a typed incomplete
reason, `result_type: pending`, the continuation job and its next automatic action. SIGKILL leaves
the published job eligible once its live flocks release. The ordinary supervisor checks fleet/CI
admission, then launches an owned fresh installed helper before model selection or GitHub reads.
The helper inherits the job lease, works only in the isolated candidate, and does not hold the
worker's round lock while building. Independent verification leaves inference scheduling available.
One continuation per worker bounds preparation concurrency. Ordinary child polling reaps results;
supervisor shutdown terminates its owned helper, preserving a durable interrupted job.

Continuation runtime status is separate from model-round status, so concurrent replay cannot
overwrite authoring progress. Repeated deadlines retain the candidate and validated phase evidence.
Phase boundaries finish metadata/source validation and index publication even if the checkpoint
margin arrives at command completion. Complete input/tool-version preparation runs at invocation
boundaries; phase checkpoints use existing input metadata and exact source/configuration checks.
Job/index leases suppress duplicate exact-candidate jobs. Permanent scope/configuration/ownership
errors become terminal incomplete results with retained artifacts, so they cannot starve inference.
Missing tools remain dormant without repeated verifier launches until local toolchain availability,
provenance or any installed verifier component changes. Toolchain probes retain separate stdout/stderr
logs. Their final version/prefix lines bind the inputs; changing Lake package diagnostics are retained
in logs and do not invalidate successful checks. A prefix must name an absolute installed directory
containing `bin/lean`. Invalid final output still leaves verification incomplete. Deadline/signal jobs remain eligible without an expiry that discards their
candidate. Terminal jobs move from `verification-pending/` into `verification-jobs/`.

### Candidate storage and automatic reclamation

Verification uses a cooperative host-local byte reservation ledger, shared across worker HOME
overrides and runtime versions. Bubble helpers mount the same ledger and record canonical host
state paths. This is storage admission, not a fixed heavy-work queue or a filesystem quota.

Default settings (positive integers except the percentage, which permits zero):

| Environment variable | Default | Meaning |
| --- | --- | --- |
| `TAUCETI_VERIFY_STORAGE_BYTES` | 128 GiB | Total reserved candidate/checkpoint storage per user and host |
| `TAUCETI_VERIFY_CANDIDATE_BYTES` | 32 GiB | Growth allowance before a candidate runs tools or copies warm caches |
| `TAUCETI_VERIFY_SOURCE_CANDIDATE_BYTES` | 2 GiB | Smaller source-preservation allowance when warm copies cannot fit |
| `TAUCETI_VERIFY_CHECKPOINT_BYTES` | 256 MiB | Maximum size of one durable source checkpoint |
| `TAUCETI_VERIFY_CHECKPOINT_POOL_BYTES` | 1 GiB | Part of the total reserved for source checkpoints during migration |
| `TAUCETI_VERIFY_OWNERSHIP_SECONDS` | 2 | Separate bound for one process-reference observation per cleanup slice |
| `TAUCETI_VERIFY_MIN_FREE_BYTES` | 20 GiB | Absolute minimum filesystem reserve |
| `TAUCETI_VERIFY_MIN_FREE_PERCENT` | 10 | Percentage reserve; the larger absolute/percentage reserve applies |
| `TAUCETI_VERIFY_COMPLETED_CACHE_SECONDS` | 3600 | Completed-job cache grace period |
| `TAUCETI_VERIFY_IDLE_CACHE_SECONDS` | 86400 | Idle pending-job cache lifetime |
| `TAUCETI_VERIFY_STORAGE_DIR` | Host-local state directory | Explicit shared accounting root; use private state for tests |

Workers sharing an accounting root should receive the same limits. The checkpoint pool is inside
the total allowance; its default scales down for small configured budgets, leaving room for one
running candidate. A historical candidate backlog therefore cannot consume checkpoint capacity. Reservations count logical
bytes, including reflinked data. Completed snapshots and checkpoints immediately reduce their
reservation to measured size, with a 16 KiB accounting floor; they do not retain a whole 32 GiB or
256 MiB allowance. Starting candidate tools reserves possible growth again. Free-space admission
counts only unwritten growth on the target filesystem, rather than counting already-written bytes
twice. If a warm snapshot cannot fit, preservation retries without generated Lake builds under the
smaller source allowance. Source copies that cannot fit return incomplete while leaving the original
workspace intact. Checkpoint archive writes have an explicit byte bound; failures retain partial
source payloads and release unused reservations. Per-item overflow returns `storage-limit`, with
limit/input guidance; an isolated oversized job stays dormant until limits or verifier inputs
change. It does not relaunch an unchanged oversized verifier every minute. Invalid settings defer
continuations without retiring jobs or charging an attempt.

Shallow discovery accounts for historical directories and candidates written by older runtimes.
Legacy candidates initially book the per-candidate allowance, and background read-only inventories
replace estimates with measurements even for partial/unknown-owner trees. Unknown ownership never
permits deleting source. A corrupt disposable ledger is quarantined and rebuilt from local directory
registrations; receipts and source payloads are not discarded.

The ordinary supervisor, activity preparer and explicit `tauceti cleanup` invoke bounded reclamation.
At most one collector runs per host, with a ten-second minimum interval; actual cadence depends on
ordinary activity/round boundaries. A separate timer is unnecessary while those services operate.
The default deletion slice is 0.25 seconds, 2048 metadata entries and 256 MiB of removal. One
process-reference observation has its separate two-second bound, so it cannot repeatedly consume
the deletion allowance. Prepared metrics report that overhead and total wall time separately. Recursive removal
has a durable cursor. Read-only inventory resumes within a long-lived collector and restarts safely
after process death. Read-only inventories retain no directory descriptors between ticks that
could masquerade as live workspace users. Collection rotates, prioritizes terminal candidates, skips already-compacted
rows, and never follows symlinks or crosses observed mounts. Job/receipt/workspace flocks and live
process references protect active work. Missing ownership or mount observations withhold that tree.

Completed candidates can shed both their own and dependency build outputs. Idle pending candidates
normally shed only their own build outputs: dependency builds stay warm to avoid repeated Mathlib
rebuilds. As a last resort, when no other cache can free capacity, other idle jobs can shed dependency
builds while the oldest runnable candidate stays warm. This prevents a protected-cache deadlock
without repeatedly evicting the job trying to finish. Disk pressure permits early reclamation. Before removing a build directory the collector checks for
tracked and untracked non-ignored files, so a source file under `build/` remains protected. Staged
state, dependency edits, Git history, checkpoints, receipts and publication/accounting journals remain
intact. Failed copies carry a durable construction marker until isolated ownership is published;
exceptions remove their partial files/reservations, and a process death leaves bounded mechanical
cleanup. A direct invocation claims its workspace before such disposal is permitted. Removed
artifacts also release their accounting, including when an entire worker was archived. A cache-generation token invalidates receipt reuse after output eviction without historical
receipt scans. Cleanup errors, including ENOSPC, remain retryable and cannot terminate the supervisor.

Retained source and protected dependency builds still count against the allowance. No unpublished
source expires merely because it is old. If these protected bytes exhaust the budget, prepared
status explicitly reports exhausted storage and the action needed: publication/validated archival or
an explicit budget adjustment. New storage allocation defers; unrelated inference remains eligible.
This deliberately does not invent permission to delete unpublished history.

Storage deferral returns `status: incomplete` and `incomplete_reason: disk-pressure`. An isolated
candidate under transient pressure gets a pending continuation that the supervisor retries without a model or verification
attempt charge for failed storage admission. Without an independent candidate, the result is terminal
incomplete with workspace retry guidance, allowing a fresh invocation instead of falsely advertising
an unusable continuation. Prepared activity status reports reservations, the allowance, free space,
compaction counts and errors; status reads never traverse candidates.

Admission is conservative for legacy input estimates; migration evicts known terminal builds
before inventory and measures unknown source without deleting it. Completed checkpoint layouts
use a bounded metadata-only migration so tiny patches do not reserve 256 MiB each.

Running candidate tools sample free space once per second and inspect at most 4096 metadata entries
per sample. Detected pressure interrupts their owned process group, records real exits and retains
pending work. A full size check before overall success also catches writers that finish between
samples. A fast writer can overshoot before detection; kernel-enforced ceilings require filesystem
quotas or a dedicated volume. The ledger and environment are cooperative controls, including inside
Bubble. Existing source records outside verification storage retain their work-recovery policy.
This change does not raise capacity, restart active rounds or change V0 helper dispatch.

Recovery builds only in an independent candidate, never resets the original source and never pushes,
creates a PR or bypasses accounting/publication guards.
Jobs store canonical candidate paths. The helper resolves runtime state/workspace symlinks before
checking that its working directory matches the job and validating the inherited job lease. Receipts
remain bound to that physical workspace, including when a runtime alias changes after deployment.
Receipts bind the actual replay environment; a different toolchain/configuration invalidates prior
evidence. Bubble recovery needs the host's normal verification tools for the pinned project; unavailable
tools leave typed incomplete evidence
and a dormant job that wakes on local tool availability changes. Publication/reconciliation of a recovered candidate remains #310's separate concern. This
verification continuation does not require #310. Claude Bash timeout variables cover the remaining
round; Codex prompts instruct the agent to wait on its returned execution session. SIGINT, SIGTERM
and SIGHUP cause owned cleanup and incomplete receipts.

Tools run in owned process groups. Interruption and deadlines terminate the group, and normal
completion sweeps leftover background tools. Cleanup is best effort: deliberately detached
descendants can escape. Host verification needs no rootless PID namespace or historical cleanup
proof. Explicit Bubble execution retains its existing outer sandbox. Normal exits check each command
directly; shell lint drivers use Bash pipefail. No process-name polling is used. Job/invocation lease
descriptors are never inherited by tools, so owner death releases ownership for recovery. Recovery
verifies in an independent workspace; publication retains its separate source, claim and CAS checks.

`TAUCETI_VERIFY_IMMUTABLE_INSTALLATIONS=/absolute/installations.json` can register trusted
installations with a mapping such as `{"/nix/store/example-lean": "publisher-revision-1"}`. Paths
must be fully resolved. The publisher must make installations immutable, including writable aliases,
and replace the installation/provenance token on updates. Registration alone grants no fast path:
Linux must report a read-only filesystem, all nested mounts must be read-only, and cold validation
rejects external symlink targets. Unsupported hosts and writable/unregistered installations retain
complete inventory and stat-keyed hash validation. A cold validation stores a content digest and
file count in a host-shared cache under the private verification cache directory, serialized per identity.
Separate worker states reuse that validated installation. Warm validation binds that digest to the root device/inode/metadata, mount topology and
publisher provenance; it performs no recursive immutable walk. A currently writable mount,
replacement root, changed mount topology or changed provenance invalidates the identity, including
at phase boundaries. A writable-to-read-only remount cycle that restores the observed topology is
not detectable: the trusted publisher must change provenance after any mutation, including writes
through aliases. Read-only mount observations alone do not establish immutability. Source/configuration,
base, executable/version and verifier checks remain exact. The trust contract and local cache are
accidental-mutation guards, not protection from an adversary changing read-only aliases or receipts.

Set `TAUCETI_VERIFY_HELPER=0` to retain/restore bundled V0 prompts at the next round. This changes
dispatch only: it does not delete receipts/checkpoints/jobs or terminate active verification.
`TAUCETI_VERIFY_CONTINUATION=0` separately pauses launching new mechanical continuations while
retaining eligible jobs and allowing active helpers to finish. Green fixture tests do not authorize
blanket fleet dispatch.

## Implementation evidence

`tests/verification.py` covers real exits, pipefail, exact and phase reuse, source mutations,
pin/base/toolchain/executable changes, deletions, renames, untracked/ignored sources, owner death,
deadlines and owned-group cleanup. Twenty independent fake verifiers must enter their builds together,
even with an old one-slot lock held and stale wait records present. `tests/verification_audit.py` adds
repeated deadlines followed by an actual successful build in the isolated candidate, Bubble namespace recovery after original deletion,
phase evidence after interruption, dispatch gating and immutable preparation costs. Its 30,000-file,
122,880,000-byte installation uses fixture read-only `statvfs` flags with the real mountinfo
reader/parser; real writable installations are also tested to fall back. Twenty contenders use
separate worker states and the host-shared validated identity cache. It prints cold and twenty-contender warm bytes, file records, stat/lstat
calls and walks, with 2,000 unrelated historical receipts. A local run observed 122,880,000 bytes,
30,000 file records and 180,204 stat/lstat calls cold; twenty warm contenders performed zero
payload reads/file records/walks and 100 stat/lstat calls, plus 40 `statvfs` calls and 40 mountinfo
reads (108,680 bytes on that host). This fixed mount-metadata cost depends on host mount topology.
These are fixture preparation costs,
not a production throughput measurement. Warm payload bytes/file records/walks
must be zero, and metadata work remains bounded independently of immutable payload/history.
All fixtures use private activity/cache/state paths and fake verifiers; they launch no paid agents.

## Deployment evidence

No fleet dispatch, capacity, spending or active-session changes are made by these fixtures. V0
remains the default. Opt-in natural canary measurements of useful completions,
copying cost and host concurrency are still required before broad dispatch.

## Natural recovery evidence

No natural production recovery or numerical throughput improvement is asserted by local fixture
results. Record observed candidate/job/receipt IDs, next automatic action, completed useful work,
verification concurrency and immutable preparation cost separately when a canary naturally selects work.

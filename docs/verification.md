# Local verification receipts and host capacity

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
full audits for broad policy/pins/deletions/unrepresentable modules, and unknown scope on missing
history. Missing canonical main fails closed with an incomplete receipt: there is no resolved base to
bind evidence to. Unknown lint scope remains incomplete instead of silently establishing lint evidence.
A stage-only build or audit does not claim the skipped requested lint succeeded.

All stages run the whole-library `lake build --iofail`, the axiom/duplicates auditor (scoped only
when safe), module-system, source style lint, changed-module environment lint, and the shim ratchet.
A stage-only call runs that stage; it does not assert that prerequisites or other stages passed.
The shim checker receives merge-base source and registry snapshots, strict `--fail-on-available`,
and `--only-new` for unchanged pins outside bump policy. Older source revisions without the checker
record an empty shim phase. CI still owns whole-library environment lint. For failures in unchanged
modules, `--lint-modules /absolute/list` adds validated existing dotted TauCeti module names to the
local lint list; its contents are fingerprinted. Use the same option for final verification. Write this
list and any extra diagnostic logs outside the checkout; untracked checkout files are inputs.

Each invocation atomically writes `state/<worker>/verification/<id>/receipt.json` before launching
anything and updates it through completion. File and directory fsync precede publication. The receipt
records requested phases, scope, start/end timestamps, original audit timing lines, tool exits,
containment capability and full log paths, including the auditor's `AUDIT_PHASES_FILE` timestamps. Reused phases refer to their original receipt.
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
Temporary shim archives are removed after use. Source metadata sampling every two seconds catches mutations restored to their original bytes; a final full digest
check rejects changed inputs. Mutable toolchain/dependency metadata is checked at phase boundaries rather
than repeatedly scanning large installations while queued. Environment hashing is limited to build
configuration and tool-search variables; provider credentials and agent-session variables are excluded. Generated Lake outputs are not source inputs. These fingerprints identify
built inputs; they do not establish proof correctness or make writable caches or local receipts a
security boundary. Verification never weakens the axiom checks.

Build, axiom/duplicate audit, module-system audit and environment lint acquire a host-shared
flock slot, released between commands and never held during the conversation. Shim and source
style checks bypass heavy admission; style runs before environment lint so it can make progress
while unrelated builds occupy capacity. `TAUCETI_VERIFY_HEAVY_SLOTS` must be a positive integer and defaults to 1. All workers
on a host should use the same setting. The default lock directory uses the real host boot/machine
identity and UID under `/tmp`, independent of worker ID, HOME, checkout or fleet labels. The directory
must be owned by the user and not writable by others. Bubble mounts the host's same directory at
`/opt/verification-queue` and persists receipts back under worker state. Other container deployments
must bind a common host directory via `TAUCETI_VERIFY_HOST_QUEUE`; isolated container filesystems
cannot coordinate flocks. The mounted directory must remain accessible with the same user ownership;
UID-remapped deployments must configure their mounts accordingly. Unsupported ownership fails the helper
with incomplete evidence, rather than aborting review rounds or V0 startup. Separate Unix users need
an explicitly shared queue arrangement. Bubble's preliminary build also uses the queue.

Waiting prints and publishes `waiting-verification-capacity`, keeps the existing candidate/claim
heartbeat running, performs no claim reads, and observes the round deadline. A short scheduler
flock serializes ticket registration and grants. Only the oldest leased ticket may acquire a free
slot; dead-owner tickets are pruned by attempting their flock. Pending jobs retain their original
age across interrupted waits. Admission is checked every 100 ms; status is refreshed at most
every fifteen seconds. Each invocation resets its wait record
and holds a separate live-invocation flock, so a cancelled helper cannot grant a later successful
invocation a stale retry refund. It does not alter
inference admission. An interrupted queue wait records unfinished work and a durable counter journal;
the worker refunds PR attempt counters idempotently during cleanup or before the next survey after
a crash. Normal completion closes the attempt journal. The loop consults fresh local journals for
host and Bubble rounds and uses exit 78 for verification-capacity waits, outside both no-work streaks
and API budget/admission accounting.

`TAUCETI_VERIFY_CHECKPOINT_MARGIN` reserves 600 seconds before the round deadline by default (a
nonnegative number of seconds; fixtures explicitly use zero). At that boundary the helper returns
incomplete with “checkpoint now”. Bubble candidate edits are saved as a base-bound binary patch and
an untracked-file archive in `verification-checkpoints/` before checks begin, including when there is
insufficient time to start verification. These artifacts are retained alongside a durable verification job in `verification-pending/`.
Queued candidates are copied to independent `verification-candidates/` workspaces before a wait is
published. Disposable Bubble candidates are preserved before a non-reused phase starts; a fully
reused invocation does not copy dependencies or Git history. Copies include local Git history,
actual source/index state and dependency sources, excluding generated Lake builds. This copying
has a storage and I/O cost that still needs canary measurement. No clone contacts a remote.

Interrupted CLI results remain `status: incomplete`, exit nonzero, and include `result_type: pending`,
the continuation job path and the next automatic action. SIGKILL leaves the already published job
eligible once its live flock releases. The ordinary supervisor consumes pending jobs before model
selection, inference admission or GitHub budget checks, under the existing per-worker round lock.
It refunds interrupted capacity charges before resetting capacity evidence. It validates the preserved
source digest, then runs the helper synchronously with a new verification deadline, without a model
conversation. Repeated queue deadlines retain job age, candidate and completed phase evidence.
Job leases prevent overlapping helpers/supervisors. Pending cooldowns stay with the mechanical owner;
blocked candidates remain recorded and are retried locally without globally blocking unrelated work.
Completed/failed jobs move out of the active queue to `verification-jobs/`; receipts and candidates remain.

Recovery builds only in an independent candidate, never resets the original source and never pushes,
creates a PR or bypasses accounting/publication guards. Receipts bind the actual replay environment;
a different toolchain/configuration invalidates prior evidence. Bubble recovery needs the host's
normal verification tools for the pinned project; unavailable tools leave incomplete evidence for
retry. Publication/reconciliation of a recovered candidate remains #310's separate concern. This
verification continuation does not require #310. Claude Bash timeout variables cover the remaining
round; Codex prompts instruct the agent to wait on its returned execution session. SIGINT, SIGTERM
and SIGHUP cause owned cleanup and incomplete receipts.

Tools use #306's `containment.Boundary`. Strong hosts contain children in owned PID namespaces;
legacy hosts accurately report best-effort process-group cleanup. Bubble owns its outer boundary and
uses the legacy inner fallback. Normal exits check each command directly; shell lint drivers use
Bash pipefail. No process-name polling is used. Lock descriptors are never inherited by tools, so
owner death releases capacity. On legacy hosts, a SIGKILLed owner can leave an escaped child running;
recovery verifies in an independent workspace. Publication of an original recovery workspace
requires #310's separate ownership gate.

`TAUCETI_VERIFY_IMMUTABLE_INSTALLATIONS=/absolute/installations.json` can register trusted
installations with a mapping such as `{"/nix/store/example-lean": "publisher-revision-1"}`. Paths
must be fully resolved. The publisher must make installations immutable, including writable aliases,
and replace the installation/provenance token on updates. Registration alone grants no fast path:
Linux must report a read-only filesystem, all nested mounts must be read-only, and cold validation
rejects external symlink targets. Unsupported hosts and writable/unregistered installations retain
complete inventory and stat-keyed hash validation. A cold validation stores a content digest and
file count. Warm validation binds that digest to the root device/inode/metadata, mount topology and
publisher provenance; it performs no recursive immutable walk. Remounts, replacement roots or
provenance changes invalidate the identity, including at phase boundaries. Source/configuration,
base, executable/version and verifier checks remain exact. The trust contract and local cache are
accidental-mutation guards, not protection from an adversary changing read-only aliases or receipts.

Set `TAUCETI_VERIFY_HELPER=0` to retain/restore bundled V0 prompts at the next round. This changes
dispatch only: it does not delete receipts/checkpoints/jobs or terminate active verification. Host
slot count and containment policy are also read at the next phase/boundary. One heavy slot is an
initial canary configuration, not a measured fleet optimum. Green fixture tests do not authorize
blanket fleet dispatch.

## Implementation evidence

`tests/verification.py` covers real exits, pipefail, exact and phase reuse, source mutations,
pin/base/toolchain/executable changes, deletions, renames, untracked/ignored sources, owner death,
deadlines, containment and accounting. `tests/verification_audit.py` adds twenty local fake FIFO
contenders (including owner death), cheap-check bypass, repeated deadlines followed by an actual
successful build in the isolated candidate, Bubble namespace recovery after original deletion,
phase evidence after interruption, dispatch gating and immutable preparation costs. Its 30,000-file,
122,880,000-byte installation uses a fixture-only read-only identity; real writable installations
are also tested to fall back. It prints cold and twenty-contender warm bytes, file records, stat/lstat
calls and walks, with 2,000 unrelated historical receipts. A local run observed 122,880,000 bytes,
30,000 file records and 180,204 stat/lstat calls cold; twenty warm contenders performed zero
payload reads/file records/walks and 100 stat/lstat calls. These are fixture preparation costs,
not a production throughput measurement. Warm payload bytes/file records/walks
must be zero, and metadata work remains bounded independently of immutable payload/history.
All fixtures use private activity/queue/state paths and fake verifiers; they launch no paid agents.

## Deployment evidence

No fleet dispatch, capacity, spending or active-session changes are made by these fixtures. V0
remains the default. Opt-in natural canary measurements of useful completions, bounded waits,
copying cost and host concurrency are still required before broad dispatch.

## Natural recovery evidence

No natural production recovery or numerical throughput improvement is asserted by local fixture
results. Record observed candidate/job/receipt IDs, next automatic action, completed useful work,
queue waits and immutable preparation cost separately when a canary naturally selects work.

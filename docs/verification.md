# Concurrent local verification and exact-input receipts

`tauceti verify [--stage build|axioms|module-system|lint|shim-expiry|all]
[--base SHA] [--json] [--no-reuse] [--lint-modules FILE]` runs synchronously in a TauCeti checkout. It exits zero
only after every requested phase succeeds against unchanged inputs. Progress, original tool output
and actual exits go to stderr; `--json` writes one `tauceti-verification/v1` object on stdout.
The default authoring/repair prompts run the shared Tools local checks, also exposed by
`lake test` and `lake lint` in current content checkouts. Receipt-helper dispatch remains opt-in. `TAUCETI_VERIFY_HELPER=1` opts a selected
natural canary into helper prompts; the helper CLI and receipt support are available separately.
Helper prompts invoke the installed absolute `scripts/tauceti-verify` wrapper with the worker's Python
interpreter. The wrapper also honors `TAUCETI_VERIFY_PYTHON` and supports Bubble's staged layout.
Wheel entry points restore its execute permission, so login-shell PATH changes cannot hide it. Existing `tauceti work`, module and checkout entry
points keep working.

The worker pins `TAUCETI_VERIFY_BASE` from the existing canonical-main checkout preparation,
`TAUCETI_VERIFY_POLICY` from surveyed labels/branch and `TAUCETI_VERIFY_STATE` to worker state. Standalone invocations resolve canonical main
from local Git refs only. No scope or verification step requests GitHub data or fetches refs.
The shared offline Python scope calculation is re-exported by `verification_scope.py`: merge-base
diff, both rename ends, staged/dirty/deleted/untracked sources, ignored sources under TauCeti,
full CI requirements for broad policy/pins/deletions/unrepresentable modules, and unknown scope on missing
history. Missing canonical main fails closed with an incomplete receipt: there is no resolved base to
bind evidence to. Unknown lint scope remains incomplete instead of silently establishing lint evidence.
A stage-only build or audit does not claim the skipped requested lint succeeded.

All stages run the whole-library `lake build --iofail`, fast changed-file axiom diagnostics,
module-system, source style lint, changed-module environment/documentation lint, and the shim ratchet.
A stage-only call runs that stage; it does not assert that prerequisites or other stages passed.
The shim checker receives merge-base source and registry snapshots, strict `--fail-on-available`,
and `--only-new` for unchanged pins outside bump policy. Older source revisions without the checker
record an empty shim phase. CI still owns whole-library environment lint. For failures in unchanged
modules, `--lint-modules /absolute/list` adds validated existing dotted TauCeti module names to the
local lint list; its contents are fingerprinted. Use the same option for final verification. Write this
list and any extra diagnostic logs outside the checkout; untracked checkout files are inputs.

The shim wrapper runs with the verifier's Python interpreter and inherited environment, including
Bubble's mounted Tools package and standalone checkers' sibling imports. Its installed TauCetiTools
implementation and configuration are verification inputs. Local helpers use the Worker's installed Tools pin; checkout launchers and CI use the aligned content pin.
Historical checkouts may retain older pins. A missing required Tools import yields
`incomplete_reason: tool-missing`, rather than a source failure. Other checker exits retain their
existing failure meaning. Recovery probes this prerequisite before claiming work or warming caches,
so an unavailable Python dependency does not launch source repair. A changed verification-tool
identity permits a fresh mechanical check of prior source failures, including records blocked only
on continuation admission; publication scope, source ownership and model authorization still apply.

Local environment lint uses Tools' shared `LocalLint.lean` checker, compiled once and
shared across workers with the same toolchain and dependency content. It imports only the
explicit module list and checks declarations owned by those modules, including documentation
and `nolint` entries. Imported dependency declarations are excluded. Empty or theorem-only
documentation scopes are valid. Older PR scripts cannot restore a whole-project documentation
scan: local verification never invokes their `lint-env.sh`. CI retains its full-library coverage.

Local axiom checks use Tools' shared `LocalAxioms.lean`, through the absolute
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
all phases. ProofWidgets' generated `widget/package.json.hash` and `widget/package-lock.json.hash`
are excluded from dependency inventories; the JSON source files remain checked. Both normal rounds
and recovery verification hydrate the shared expanded-artifact cache before verification starts.
Complete inventories are stored once under `verification-inputs/<digest>.json`, referenced
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
completes preparation. Completion markers live separately in `verification-hash-roots/`, with one exact lookup per
workspace and no history scan. Warm preparation in migrated workspaces reads only V2, including
after source changes. A toolchain switch may require one new payload hash
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
candidates. The existing source/import guards and new-file length check run before the default build.
Source style and environment lint run after their required artifacts exist. These checks use
the shared Tools implementation; no GitHub request is added to local scope or lint.

`TAUCETI_VERIFY_CACHE_DIR` selects the shared immutable-installation identity cache. The default
uses the host boot/machine identity and UID under `/tmp`. The directory must be owned by the user
and not writable by others. Bubble mounts it at `/opt/verification-cache` and persists receipts
separately under worker state. Cache locks protect creation/validation of one installation identity;
they are released before expensive verification commands run. Unregistered writable installations
retain complete inventory validation without requiring a shared cache. If the shared cache is
unavailable or unsafe, host verification falls back to complete validation and Bubble uses a
private cache directory. Cache availability never grants success or prevents the checks from running.

Verification runs to completion inside the round's own checkout and writes its receipt there;
publication guards read that receipt. There is no checkpoint margin, no durable verification job
and no copy of the workspace: verification never continues past the round. If the round is killed
(the loop's `TAUCETI_ROUND_TIMEOUT` hard safety cap, a signal or owner death), the receipt stays
`status: incomplete` and the work is abandoned. Bubble candidate edits are saved as a base-bound
binary patch and an untracked-file archive in `verification-checkpoints/` before checks begin.

Older releases kept continuation state under worker state (`verification-candidates/`,
`verification-pending/`, `verification-jobs/`, `verification-job-index/`,
`verification-copy-incomplete/`, `verification-storage/` and `verification-storage-generations/`,
including under Bubble's `verification-state/`). Each round start and the cleanup collector delete
it through the ordinary retention removal, which never deletes through a symlink, never removes a
mount point and leaves a tree in use by a live process for a later pass. It is never migrated.

Incomplete CLI results exit nonzero with a typed incomplete reason and `result_type: incomplete`.
Toolchain probes retain separate stdout/stderr logs. Their final version/prefix lines bind the
inputs; changing Lake package diagnostics are retained in logs and do not invalidate successful
checks. A prefix must name an absolute installed directory containing `bin/lean`.

### Checkpoint storage

The host-local ledger records Bubble source checkpoints across worker HOME overrides and runtime
versions; bounded housekeeping measures them. Rows an older release recorded for verification
candidates are dropped. Only actual filesystem headroom can defer or interrupt verification.

| Environment variable | Default | Meaning |
| --- | --- | --- |
| `TAUCETI_VERIFY_MIN_FREE_BYTES` | 20 GiB | Actual minimum filesystem headroom |
| `TAUCETI_VERIFY_MIN_FREE_PERCENT` | 10 | Percentage headroom capped at the worker critical reserve (at most 20 GiB); explicit `MIN_FREE_BYTES` remains a minimum |
| `TAUCETI_VERIFY_STORAGE_DIR` | Host-local state directory | Shared observation root; use private state for tests |

Running tools return `incomplete_reason: disk-pressure` only for actual low filesystem headroom.
Ownership/cleanup lease contention is separately typed `verification-busy`. If ENOSPC prevents
housekeeping, the collector may discard optional V2 stat-hash metadata under its writer lock and
retry once.

Tools run in owned process groups. Interruption terminates the group, and normal completion sweeps
leftover background tools. Cleanup is best effort: deliberately detached descendants can escape.
Normal exits check each command directly; shell lint drivers use Bash pipefail. SIGINT, SIGTERM and
SIGHUP cause owned cleanup and incomplete receipts.

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
dispatch only: it does not delete receipts/checkpoints or terminate active verification. Green fixture tests do not authorize
blanket fleet dispatch.

## Implementation evidence

`tests/verification.py` covers real exits, pipefail, exact and phase reuse, source mutations,
pin/base/toolchain/executable changes, deletions, renames, untracked/ignored sources, owner death,
and owned-group cleanup. Twenty independent fake verifiers must enter their builds together,
even with an old one-slot lock held and stale wait records present. `tests/verification_audit.py` adds
dispatch gating and immutable preparation costs. Its 30,000-file,
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

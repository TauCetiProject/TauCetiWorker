# Shared extracted Mathlib builds

Host coding rounds can reuse one extracted Mathlib build per dependency/configuration
key. Source files, TauCeti outputs and changes to Mathlib outputs stay private to a
worker. Reviews retain their existing execution path.

Sharing remains **off during the #364 cutover**. This change does not authorize reactivation.
Sharing defaults to `off` without an opt-in policy or explicit environment mode.
An unreadable or invalid policy fails to private mode. `off` uses ordinary private
cache preparation; `auto` attempts sharing with private fallback. `required` refuses to start inference if sharing is unavailable.
`TAUCETI_SHARED_MATHLIB_ROOT` selects the local snapshot pool; it is pinned to the
login user's home before worker HOME isolation. An optional comma-separated
`TAUCETI_SHARED_MATHLIB_WORKERS` allowlist supports a bounded rollout to existing
workers without changing admissions or adding a ramp command. For an existing
installation whose long-lived worker wrappers cannot acquire new environment
variables yet, `policy.json` in the pool accepts `mode`, a `workers` list, and an
optional `expires_at` UTC epoch with `expiry_mode` (default `off`). This is read at
round boundaries; environment overrides take precedence. Expiration prevents new
shared rounds after a bounded canary without signalling active rounds.

Run `tauceti doctor` to exercise Linux user namespaces, bubblewrap overlay support
and private copy-up. No root daemon is required. Unsupported kernels, disabled
user namespaces, missing bubblewrap, and unsuitable filesystems produce an
explanation and an administrator action where appropriate. Automatic mode falls
back to ordinary private cache warming; required mode pauses before inference.

## Publication and private writes

The key includes pinned dependency revisions, the Mathlib manifest, the root Lake
configuration and manifest, Lean toolchain and ABI, architecture and platform,
resolved toolchain location and Init artifact digest, external `LAKE_CONFIG` content,
and the dependency Git remote/branch and environment context that select Mathlib artifact caches.
The downstream project branch/fork and fetched tag descriptions do not split
snapshots. Mathlib selects its cache by the dependency remote/tracking context,
not by `name-rev` tag descriptions.
Temporary config filenames and per-worker download directories do not split keys;
cache-selection values are hashed to avoid persisting endpoint credentials.
Remote URLs are hashed in published metadata; credentials are not copied there.
Dirty or unpinned dependencies use private builds. A miss makes fresh local git
clones at those revisions, downloads using `lake exe cache get`, and verifies
Mathlib `.olean` and `.ilean` outputs before syncing the immutable tree and publishing it atomically. It never publishes
an agent's mutable build directory. A per-key publisher lock serializes builders separately from reader leases, so
simultaneous first users can enter as soon as publication finishes. A ready receipt
also records regular-file inode, mode, link count, size, mtime and ctime.
Device numbers are checked for consistency within the tree, without persisting
boot-dependent device IDs. Files with hardlink aliases are rejected. Every reuse checks
that inventory without rereading all artifact bytes. This detects missing/replaced
files and in-place corruption, including same-size writes with restored mtime.
Links and special files are rejected, including symlinked directories. Unsupported
fresh extraction uses private fallback; it never becomes a lower. A damaged
published lower is preserved for evidence and writes a separate `safety-stop.json`,
forcing future auto rounds off even with an environment auto override. Required
mode fails explicitly. A policy rewrite or expiry cannot clear the latch.
Investigate, then explicitly remove the stop file and restore the intended policy
mode; stopping also writes policy mode off for compatibility with older releases.
Both writes are attempted independently, followed by a directory sync. Failures
are logged explicitly and recorded when possible; inability to persist the stop
never replaces a paid outcome or prevents upper restoration.

A shared agent runs inside a rootless bubblewrap mount namespace. It deliberately has no
PID namespace: agents and their tools see ordinary host PIDs and `/proc`, because tools
that report a PID to a service outside the namespace (systemd, D-Bus) break otherwise.
The published pool is read-only inside the view and trusted extraction keeps only its staging
project writable. Ordinary private agents, private cache fetches and verification tools run
outside this mount view and can write to the pool, so sharing must
stay off on a host with private writers. OverlayFS combines the shared Mathlib lower with that worker's
fresh private upper per round; in-place writes copy up. The entire coding process and
its tools see this view. Networking, credentials and the existing cancellation
lifecycle are retained. This is build storage isolation, not an additional security
boundary for untrusted code. Processes outside the view (private agents and tools,
and pre-deployment active agents), other writable filesystem aliases, and the host
owner can bypass the mount protection. Keep sharing off on a host
with such writers; do not terminate paid agents to establish this condition.
Lower file modes permit private copy-up writes; changing them to read-only would
also prohibit ordinary upper writes through OverlayFS.

At every idle round boundary, the previous shared delta moves aside and a fresh
empty upper starts, so old extracted copies, whiteouts and opaque directories
cannot shadow the shared lower. First activation also moves the previous private
Mathlib build aside. The backup survives failed activation;
it is removed after a successful invocation. Toolchain/dependency changes made by
an agent affect private files; the following round selects a new key. Disabling
sharing moves the sparse private delta aside, starts an empty private build, and
reconstructs outputs through normal cache preparation. Sparse deltas and whiteouts
are never reused outside their overlay. Cache warming still runs inside the view
to prepare other dependencies, skipping already verified Mathlib outputs. A bwrap
setup failure uses private fallback; a verified artifact missing through the fresh
overlay stops sharing.

Running readers hold shared leases; the sandbox also retains the lease descriptor
so collection cannot race a surviving process. Trusted staging downloaders never
inherit the exclusive publication lease; only completed shared views pass reader
leases to children. The optional mount view has its own capability check; it does not consult
round containment policy, historical process markers or quiescence proofs. Lock waits are bounded to 25 minutes (the normal
extraction timeout is 20 minutes), then auto falls back privately and required
mode fails with an actionable reason. Cleanup removes unused snapshots
after seven days, or oldest unused snapshots under disk pressure, until at least
10% disk space is free. Leased entries are skipped. An unused entry moves atomically
to a synced trash directory before deletion; interrupted deletion cannot leave a
partial lower in the published namespace. A pool-level collector lock serializes
trash reclamation and entry selection. Subsequent collection reclaims trash. Unlocked staging trees left by killed builders are
reclaimed after four hours, before useful snapshots are evicted for disk pressure. Publication refuses to leave
less than 10% free if no unused entries can be reclaimed.

The existing activity preparer collects small local preparation receipts. Status
reads only that prepared summary, including sharing/private modes, cache hits,
preparation time and fallback reasons. A mounted overlay remains "preparing"
until cache warming succeeds; a failed warmer is reported as blocked and never
starts inference. `results.jsonl` in the pool contains
content-free coding invocation results, cleanup phase receipts and native round IDs
for joining the existing round/phase/resource records. A released shared view
becomes idle rather than continuing to count as a current shared worker. Cleanup
of a view is separate from completion of a full work round. Cleanup records body
and backup-removal errors and retained backups;
a successful invocation does not imply successful cleanup. Backup-removal errors
preserve the paid invocation result. Supervisor PID/start/boot identity prevents
stale per-worker preparation from counting another worker's lease as its own.
Receipts include upper file count/bytes and stop-write outcome.
Unregistered and pre-deployment writers still require operator verification.

Existing `resources.jsonl` round receipts now include `read_bytes` deltas from
`/proc/self/io` for the supervisor and reaped children, and host-wide CPU, memory
and I/O PSI totals as `host_pressure_stall_us`. Live/unreaped descendants are not
included in storage-read accounting; host pressure includes co-tenants. Fields
are unknown when unavailable. Join resource `activity_id`, shared receipt
`round_id`, and existing audit/round phase completion before comparing runs.
The [Linux I/O accounting manual](https://man7.org/linux/man-pages/man5/proc_pid_io.5.html)
describes the kernel read counter and its scope.

## Rollout

Sharing stays off throughout the #364 cutover. Any later rollout belongs to the existing
rollout owner at idle boundaries; the optional procedure below is for that later decision.
Every private writer on the host, including an `auto` worker that falls back after a capability,
space or lock failure, can bypass the shared view. Do not activate sharing alongside those writers.
`auto` provides private fallback, not host-wide write isolation; the inventory detects later
mutation but cannot protect readers that already hold a lease. Reactivation must account for
that limitation without relying on the removed global containment policy or status markers.

Keep funding and worker capacity unchanged. Use only naturally selected work;
do not trigger CI, run synthetic benchmarks, start paid measurement sessions or
restart active paid agents. Normal implementation PR checks still apply.

1. Enable one existing worker with pool `policy.json`, mode `auto`, a one-worker
   `workers` allowlist, `expires_at` set to now + 86400, and `expiry_mode: "off"`.
   Environment mode/allowlist overrides take precedence during ordinary rollout;
   inspect them before selecting the worker. Policies are read at natural round
   boundaries. Off/private remains the rollback path.
2. After a natural **full work round** completes with verified overlay isolation,
   no preparation failure and successful cleanup, expand to five existing workers
   and renew the 24-hour expiry to off.
3. After five successful natural rounds across that group with no sharing-related
   failures, allow all current workers, again retaining a 24-hour expiry to off
   until explicitly accepted. Do not increase the worker ceiling or budget.

For each gate retain the shared preparation/result/cleanup receipts, round ID,
audit phase completion, actual storage read bytes and resource stalls, together
with an existing comparable private run. Use existing local phase/resource logs
and kernel observations; missing measurements stay unknown. RSS and worker counts
are not evidence of page-cache savings or speedup. Record toolchain/pin, task,
cache warmth, co-tenants and disk pressure as confounders.

On any isolation, integrity or lease failure set future-round policy off
immediately. Preserve the lower and evidence, all active leases/readers, usage
journals and the append-only budget ledger. Never delete an active lower or kill
paid agents to roll back. Lower-integrity, demonstrated probe-isolation and lease-lock
failures latch this stop automatically;
other observed isolation/lease failures require the same operator rollback. An unsupported capability or insufficient space in auto uses
private preparation; required mode refuses inference with the reason.

The [historical #311 validation record](evidence/shared-mathlib-311.json) predates
#364. Its containment deployment gates are retired; it establishes neither
deployment nor reactivation readiness for this baseline.

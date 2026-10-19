# Shared extracted Mathlib builds

Host coding rounds can reuse one extracted Mathlib build per dependency/configuration
key. Source files, TauCeti outputs and changes to Mathlib outputs stay private to a
worker. Reviews retain their existing execution path.

`TAUCETI_SHARED_MATHLIB_BUILDS=auto` is the default. `off` uses the ordinary private
cache preparation. `required` refuses to start inference if sharing is unavailable.
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
and the Git remote/branch/tag context that selects Mathlib artifact caches.
Remote URLs are hashed in published metadata; credentials are not copied there.
Dirty or unpinned dependencies use private builds. A miss makes fresh local git
clones at those revisions, downloads using `lake exe cache get`, and verifies
Mathlib `.olean` and `.ilean` outputs before syncing the immutable tree and publishing it atomically. It never publishes
an agent's mutable build directory. A per-key lock serializes builders.

Each agent runs inside rootless bubblewrap mount and PID namespaces. The published pool
is read-only there. OverlayFS combines the shared Mathlib lower with that worker's
persistent private upper; in-place writes copy up. The entire coding process and
its tools see this view. Networking, credentials and the existing cancellation
lifecycle are retained. This is build storage isolation, not an additional security
boundary for untrusted code.

At an idle round boundary, first activation or a key change moves the previous
private Mathlib build aside and starts with an empty upper. Duplicate old artifacts
therefore cannot shadow the shared lower. The backup survives failed activation;
it is removed after a successful invocation. Toolchain/dependency changes made by
an agent affect private files; the following round selects a new key. Disabling
sharing moves the sparse private delta aside, starts an empty private build, and
reconstructs outputs through normal cache preparation. Sparse deltas and whiteouts
are never reused outside their overlay. Cache warming still runs inside the view
to prepare other dependencies, skipping already verified Mathlib outputs.

Running readers hold shared leases; the sandbox also retains the lease descriptor
so collection cannot race a surviving process. Cleanup removes unused snapshots
after seven days, or oldest unused snapshots under disk pressure, until at least
10% disk space is free. Leased entries are skipped. Unlocked staging trees left by killed builders are
reclaimed after four hours, before useful snapshots are evicted for disk pressure. Publication refuses to leave
less than 10% free if no unused entries can be reclaimed.

The existing activity preparer collects small local preparation receipts. Status
reads only that prepared summary, including sharing/private modes, cache hits,
preparation time and fallback reasons. A mounted overlay remains "preparing"
until cache warming succeeds; a failed warmer is reported as blocked and never
starts inference. `results.jsonl` in the pool contains
content-free coding invocation results and native round IDs for rollout checks.

## Rollout

Keep funding and the worker ceiling unchanged. Enable two existing workers first;
observe at least five successful Mathlib coding rounds and thirty minutes before
expanding. If five rounds have not completed within sixty minutes, stop the canary
as inconclusive. Do not manufacture paid sessions or interrupt active rounds to
obtain a result. Roll back at subsequent round boundaries with mode `off`; retain
all usage journals and the append-only budget ledger.

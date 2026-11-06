# Produce less temporary data and reclaim it when ownership ends

Avoid redundant trees, dispose of temporary data at existing ownership transitions,
and make the existing collector finish useful deletion work. Keep periodic discovery
for crashed owners and reusable cache eviction. Add no cleanup daemon, storage
registry, reference-counting service, admission gate, or operator acknowledgement.

This is an implementation plan. Preserve normal worker and CI admissions, useful
warm workspaces, source recovery, and automatic retries.

## Today's scope

The full retention survey is recorded in
[TauCetiWorker #597](https://github.com/TauCetiProject/TauCetiWorker/issues/597).
Keep accounting/history archival, a complete steady-state storage budget, installed
version retention, and broader dependency sharing in that follow-up.

Today's work targets abandoned and terminal data that serves little remaining
purpose:

1. Finish disposal of inactive obsolete/blocked recovery materializations and
   terminal verification candidates. Existing failed/content-exhausted recovery
   should reach a terminal abandonment disposition instead of being scheduled
   indefinitely. Do not make deletion depend on reviving historical work.
2. Drain the existing deletion queue across bounded slices, retain requests received
   during collection, and keep cleanup's runtime/root selection correct on upgrades.
3. Close confirmed review scratch lifetime holes at the actual leaking caller.

The operator accepts abandoning troubled tasks and letting fresh agents write new
PRs. Use existing failure/content allowances and terminal/discard paths; add no new
age threshold, recovery service, or adoption campaign. A transient claim/capacity
wait alone is not an exhausted task. Keep currently running work and active handoffs
with their existing owner. Abandoning source repair must not replay an uncertain
publication or erase the small intent/accounting evidence needed to reconcile it.
When the existing source lifetime expires, release derived recovery checkouts but
retain immutable source records and uncertain publication intent as evidence.
Their eventual history archival remains in #597.

Prefer an existing compact source record and a small abandonment reason over a
full retained checkout. For explicitly abandoned work, preserving every unfinished
Git operation or dependency edit is not a prerequisite to releasing its private
materialized tree. Keep those distinctions inside the existing task lifecycle;
do not weaken ownership checks on active or unrelated work.

## Evidence and existing mechanisms

On 6 November 2026, tauchonki had about 6.5 GiB free on its 3.5 TiB root filesystem.
The cleanup service used an old runtime and data root while workers used another.
Emergency remediation processed caches from 40 inactive workspaces and removed 214
inactive, Git-clean Mathlib review input copies. Headroom temporarily reached about
606 GiB while builds continued; that is observed free space, not a per-deletion
physical savings estimate. A temporary host override completed all seven discovered
roots. Receipts are under `~/.local/state/tauceti-fleet/disk-cleanup-roots/`.

Recheck deployed revisions before implementation: this checkout predates some
installed data-root and native Lake changes. Build on the existing mechanisms:

- [Recovery housekeeping](../../tauceti_worker/recovery_maintenance.py) already
  retires terminal records through `discard_record()`, renames them into
  `recovery-cleanup/discarded/`, and removes them with bounded deletion journals.
  `defer_caches()` already registers disposable recovery caches.
- [Retention](../../tauceti_worker/retention.py) has a cleanup lock, asynchronous
  wake requests, and a fallback timer. It currently runs one recovery deletion
  slice per invocation; requests to an already-running oneshot do not start
  another pass.
- [Verification](../../tauceti_worker/verification_jobs.py) already uses reflinks
  and validated immutable package references where supported. Its
  [storage collector](../../tauceti_worker/verification_storage.py) compacts build
  outputs but can leave whole source candidates permanently marked compact.
- [Source records](../../tauceti_worker/work_recovery.py) preserve bundles,
  patches, untracked source, and manifests, and reuse matching records.
- [Shared repository snapshots](../../tauceti_worker/shared_repositories.py) have
  immutable trees and host process leases. Managed reviews already use ephemeral
  Bubble containers with teardown in the Worker's completion path.

## 1. Keep cleanup on the same roots and supported code as workers

Fix installation and upgrades first. Every project/pool/worker keeps a stable data
root when code changes. Carry the existing data-root selection through round,
verification, recovery, and cleanup subprocesses. Re-render the cleanup unit during
runtime upgrades, rather than only during initial service installation, so its
executable, imports, and data-root selection follow the supported deployment.
During mixed-version operation, preserve the running workers' selected roots.

Use the same per-worker selections as the launcher, plus the existing legacy root
registry. Discover newly selected roots on later upgrades; do not freeze today's
seven-root list. Reuse deployed `legacy_data_root` and migration support. Do not copy
or relocate live data, create another mapping database, or treat an upgrade as
permission to expire a warm cache generation. Runtime pruning must preserve data
still stored under installation directories and their referenced aliases.

Legacy roots may contain unfinished work, not just history. Inspect existing
recovery inventory and pending verification/outbox records in the same pass.
Separate active consumers and uncertain publication from failed, exhausted, or
permanently blocked source-repair attempts. Retire the latter through the existing
abandonment/discard path; do not start consumers merely to rescue all old work.
Apply the same existing 24-hour source lifetime to owned retained checkouts even
when Git/source preservation never succeeds. Keep their small owner/publication
metadata while retiring the private tree under the existing ownership checks.
Still-useful active work keeps its original claims and authorization.

A root can leave compatibility discovery after its remaining active consumers and
publication/accounting obligations are resolved. An abandoned source-repair task
does not keep its materialized checkout or data root alive indefinitely. This
accounting governs root retirement, not whether unrelated workers may continue.

## 2. Drain existing deletion work when requested

Keep the existing collector, cleanup lock, per-record locks, and deletion journals.
Inside one invocation, repeat bounded recovery and discarded-tree deletion slices
while they make progress. Reuse existing scheduling/cursors, rotate past blocked
owners, and give roots a fair turn. Continue inventories from the same process
instead of restarting them each slice or repeatedly inventorying the shared pool.

Call `request_cleanup()` when existing disposal transitions queue work, including
`discard_record()`, `defer_caches()`, and owned review scratch disposal. Persist new
demand even when service-start requests are coalesced. Before exiting, check demand
that arrived during the run and make another useful pass. Requests during an active
oneshot must not disappear behind the current five-minute start debounce. The
fallback timer also recovers a crash or a request arriving as the service exits.

Bound individual slices with existing budgets and keep progressing in the
background. Do not introduce a new overall runtime cutoff, job-age gate, global
work lock, or independent collector. Stop or yield when existing scheduling says no
eligible work can progress. A new request may get one final pass after progress stops. After that full round
makes no eligible progress, return even if low-space admission keeps requesting cleanup;
repeatedly observing the same blocked owners is not progress. Cleanup failure
cannot reverse useful completed work.

Use existing store-specific restart behavior: recovery cache reuse invalidates old
intent through `cache-use.json`; verification waits for its journaled partial
removal to finish before reusing that candidate. Do not replace these with a new
universal generation protocol. A younger replacement at a pathname must survive.

Keep regular worker checkouts and pending continuations warm. Prompt retirement
applies to genuinely removed/retired workers after their last consumer exits, and
to finished disposable jobs. A code revision change is not a retirement event.

## 3. Give review scratch a clear outer owner and share Mathlib inputs

### Dispose of scratch at the owning process boundary

Trace which launch path produced the host `/tmp/tauceti-review-*` leftovers first.
Those copies do not establish that managed Bubble reviews leaked host scratch:
`run_in_bubble()` already requests an ephemeral container and tears it down in
`finally`. Confirm that teardown removes its private workspace and reuse that
ownership boundary. Do not add a host-backed scratch mount to a path already
reclaimed by container teardown.

For managed host scratch that survives the reviewer subprocess, let the Worker
allocate a fresh directory under its existing worker state and pass `--workdir`.
After all consumers exit, rename that uniquely owned tree into the existing
`recovery-cleanup/discarded/` directory and request collection. At the next owned
review boundary, stale directories can use the same path under the existing round
and process ownership checks. A rename into discarded storage is sufficient;
there is no need for a second scratch registry or generation scheme.

For standalone TauCetiReview, wrap automatically allocated scratch in a context
manager or `finally` so setup failures, early returns, and ordinary exceptions do
not bypass removal. Honor explicit `--keep` and caller-owned `--workdir`. Preserve
persistent review state, publication outboxes, accounting, and required results
outside disposable scratch before releasing it. SIGKILL/reboot leftovers use the
fallback collector's known owned roots, not routine broad scans of `/tmp`.

### Avoid copying Mathlib into every review

Extend the existing shared repository snapshot facility to select an exact commit.
Use one immutable Mathlib source tree per pinned revision in the existing sharing
domain. Keep the existing host process lease for the full reviewer/container
lifetime; container paths and the collector's host mount table cannot by themselves
establish whether a container still uses the tree.

Replace `_review_mathlib_source()`'s inline pruning of all but the two newest trees
with the snapshot facility's existing unpinned retention. A newer revision does
not make an active input disposable. Mount the selected tree read-only at the
reviewer's expected Mathlib path, or pass a supported absolute path. Do not rely
on a directory symlink: ordinary `find` and recursive search tools may not follow
it and could incorrectly report missing declarations. Teach TauCetiReview to use
an input already mounted in place instead of copying it or rejecting path overlap.
Test the actual reviewer search commands and existing sandbox restrictions.

Keep private fallback where immutable sharing cannot preserve isolation. Defer
roadmap sharing and broader verification dependency sharing: the incident does not
establish their benefit, and verification already has reflink and immutable-package
paths. Follow the [machine Lake cache plan](machine-lake-cache.md) for artifact
sharing; add no second cache protocol or overlay system here.

## 4. Retire whole terminal verification candidates

A compacted candidate must not retain an independent Git clone and dependency
sources forever merely because its generated builds were removed. When the
existing job/publication lifecycle has no remaining consumer, register the candidate
with the existing journaled deletion path under its job, lease, and storage locks.
The completed isolated candidate is a disposable copy: keep the original source,
source checkpoints, and receipts outside it rather than creating a duplicate
recovery record. Do not overwrite an existing recovery consumer while retiring a
verification copy.
Retain receipts and publication/accounting evidence required after tree removal.

This is distinct from terminal recovery retirement, which already exists. Connect
verification retirement to that machinery and wake collection rather than
reimplementing recovery disposal. An active continuation is not disposable. A failed
attempt becomes disposable when the existing task policy abandons it; do not turn
an exhausted allowance into permanent pending work merely to retain its checkout.

Preserve edited dependencies explicitly. The general recovery remover can remove
an untracked top-level `.lake` before archiving the parent, and the parent source
record does not capture dependency edits inside `.lake/packages`. Before handing a
verification candidate to that path, use existing Git/source inspection to park
package edits or other source that the recovery format cannot represent. Do not
let blanket `.lake` eviction run ahead of that decision. Use existing parking
and retry behavior for work that still needs preservation. Explicitly abandoned
task-owned source can instead follow the abandonment disposition above. Directory
names alone do not establish either ownership or abandonment.

## 5. Retain only necessary periodic discovery

The same background entry point handles explicit discarded trees, known crash
leftovers/construction markers, and shared cache eviction. Ordinary completion
feeds exact owned paths into it. The timer covers missed disposal after crashes,
service downtime, and reusable data whose future consumers are unknown. Keep
legacy root discovery until those roots are accounted for; do not add routine
whole-disk or `/tmp` scans.

Preserve existing retention defaults and operator overrides, including private-cache
age, managed-cache size, and verification idle/completed-cache policies. Report their
effective values. Optional native Lake targets that are currently unset remain
unset until configured; that does not remove defaults from other stores or block
these lifetime fixes. Pinned artifacts and protected source may prevent a target
from being reached. Cache eviction retains existing refetch/private fallback.

Reuse current counts and receipts, adding the missing per-root pending-work counts
and observed filesystem free space before/after collection. Keep status rendering
free of tree walks. Existing inode accounting is approximate for XFS reflinks;
validate physical headroom with filesystem free space. Defer new reporting systems
and sweep-frequency tuning until these changes show a remaining need.

## Delivery and validation

Ship collector progress and abandonment/whole-candidate disposal as the urgent
changes, preserving the working root-coverage override until installation/upgrades
provide the supported equivalent. Fix the confirmed review scratch producer next.
Mathlib input sharing is independent follow-up work and must not delay disposal.
Update Worker and TauCetiReview through their normal revision/pin flow. No broader
dependency-sharing or history-archival rollout is required today.

Extend existing tests at the changed boundaries:

| Scenario | Required result |
| --- | --- |
| Runtime upgrade and pruning, including a mixed-version fleet | Cleanup follows supported code, includes every selected data root, and preserves live data without rebuilding a fleet of caches |
| Failed/exhausted work, active verification, and an unsynced publication coexist in an old root | Abandoned source-repair trees are released; active ownership and publication evidence survive without a historical recovery campaign |
| Backlog needs several slices; another disposal request arrives during collection | Deletion continues across slices and observes new demand; blocked work does not starve other roots |
| Review setup/engine/publication fails or its owner dies | Existing sandbox teardown or owned scratch disposal removes temporary data; persistent results/outbox survive |
| Concurrent reviews use different pinned Mathlib revisions while snapshots refresh | Each input stays available through host ownership; real reviewer searches see the mounted sources |
| Terminal candidate is compacted and no longer referenced | Required source is preserved once and the whole materialized tree is eventually removed |
| Candidate contains dependency edits, pending work, or a partial deletion journal | Source is parked or existing continuation/removal behavior applies; no rebuild loop or stale-intent deletion |

Use focused integration fixtures with real processes where ownership, exit, mounts,
and upgrade behavior matter. Retain existing native build/isolation tests. Deploy
live with current data paths and admissions, verify actual running revisions and
useful completions, and repair interrupted or stranded work within scope.

Today's completion means abandoned and terminal materializations stop accumulating,
the existing collector drains useful disposal work, and confirmed scratch leftovers
are removed at their owning boundary. Active useful work and publication/accounting
correctness survive. Longer-term local-history bounds remain tracked in #597;
this urgent work does not claim complete steady-state storage bounds.

# Retained build trees: implemented

Finished generated build deltas and rollback copies are disposed of through the
existing owned cleanup queues. Source, publication evidence and actively leased
inputs retain their existing protection. No fleet drain or new retention limit is
required.

[Worker #600](https://github.com/TauCetiProject/TauCetiWorker/pull/600) implements
producer-owned disposal, including repeated sharing-off preparation, stale markers,
rollback failures and cold private verification. It is merged and deployed.
[Worker #598](https://github.com/TauCetiProject/TauCetiWorker/pull/598) supplies the
underlying lifetime and cleanup paths.

Production acceptance on 7 November 2026 covered all seven known current and
historical Worker data roots. The observed discard queues drained with no reported
collector errors. Both live workers subsequently completed and published useful
work, including PRs #29989 and #30153 on deployed Worker revision `8a00352ae`.
Normal worker and CI admissions stayed enabled.

The one-time legacy review-source cleanup removed 78 abandoned Mathlib leaves
while preserving their parent directories. Their named allocation was
12,236,648,448 bytes; this is not a measurement of exclusive physical extents.
The previously discussed PR #27907 recovery checkpoint is also absent.

There is no remaining implementation step in this plan. Ongoing cleanup is the
normal collector's job, including interrupted deletion and release of readers at
their own round boundaries. A protected verification or recovery record is not
permission to delete its source or publication evidence.

Operational receipts are under
`~/.local/state/tauceti-fleet/storage-plans-implementation-20261107/` on tauchonki.
See [source sharing](shared-workspace-dependencies.md) for the new review-input
path and [cache retirement](expanded-cache-retirement.md) for the final cache roles.

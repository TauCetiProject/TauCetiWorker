# GitHub state storage recovery

The repository Durable Object has a 10 GiB SQLite limit. At the limit,
Cloudflare still serves reads and deletes, but inserts and updates fail. The
service is built so that this state degrades writes only:

- Startup on a current schema performs no writes. A migration runs in one
  transaction; if it cannot commit, an existing compatible schema stays
  readable (`schema: "legacy"`). A genuinely missing schema returns
  `schema_unavailable` with the missing tables, never empty data.
- Complete stored reads do not depend on demand, metrics, refresh scheduling,
  or derived-cache writes. After the first storage-full failure the object
  enters in-memory degraded mode: optional writes, repair dispatch, survey
  hydration and snapshots stop, so no GitHub quota is spent on results that
  cannot be stored. Responses keep their original observation timestamps and
  completeness; unknown coverage stays `status: "unavailable"`. Degraded
  responses carry `X-TauCeti-Storage: degraded`. Clients do not judge data
  age, so once a degraded mirror has not changed (no applied webhook or
  stored repair) for ten minutes, queries return `status: "unavailable"` with
  reason `mirror_frozen` and existing clients use their direct GitHub
  fallback instead of acting on frozen state.
- New continuation pages are written to private R2 (`cursors/<repo>/<id>/<offset>.json`),
  using the existing `id:offset` cursor syntax and the same repository,
  query, coverage and expiry checks. Legacy SQL cursors remain readable until
  they expire.
- Expired cursors, metrics, journal entries and retired-object markers are
  deleted at the start of every alarm (and on the cron tick while degraded),
  before any repair, hydration or snapshot work.
- Errors keep their established codes (`storage_unavailable`,
  `service_unavailable`) and add `reason` and a sanitized `request_id` that
  also appears in the structured logs.

## Parked ingress

Signed webhooks are always accepted into R2 and the queue. The consumer
acknowledges a message only after one of these is durable:

1. the repository committed its delivery receipt, or
2. a parked pointer and a pinned, digest-verified body exist:
   `parked/<repo>/pointers/<received>-<delivery>.json` and
   `parked/<repo>/bodies/<delivery>.json`.

A `storage_full` or `schema_unavailable` response parks immediately. Other
failures retry with exponential backoff (5 seconds to 10 minutes) and park on
the tenth attempt, well inside the queue's 20-retry budget. A failed parking
write is retried with the same backoff and never acknowledged. Parked objects
carry no expiry metadata, so normal R2 retention keeps them.

The cron drains parked pointers for up to twenty seconds of each minute, in
pages of 100 replayed in concurrent groups of ten, oldest page first.
A pointer is removed only after the repository commits the receipt. A crash
after commit repeats a replay that the receipt table deduplicates. An item
that fails replay five times for a non-storage reason moves to
`parked/<repo>/held/` so it cannot block newer events; it remains replayable.

Setting the ingress mode to `parked` (planned migration, for example
https://github.com/TauCetiProject/TauCetiWorker/issues/316) parks every
delivery without calling the repository object, and stops replay until the
mode returns to `apply`. The repository object is the barrier: once the admin
call returns `drained: true`, it refuses every new application (including
batches that read the earlier mode) and no application is still in flight.
Receipts are retained for fourteen days after application, so a replay of a
long-parked event still deduplicates its own cleanup.

The cron delivery audit, which reads and redelivers from GitHub, is skipped
while the repository is degraded or ingress is parked.

## Operator procedure

All commands use the admin token, are bounded, and send no GitHub requests.
Use `--environment test` first; the test environment also enables
`FAULT_INJECTION`, which can simulate a full database
(`POST /admin/storage/fault {"mode":"full"}`) and persists across restarts
until set back to `none`.

```sh
S=services/github-state/scripts/storage_recovery.py
python3 $S --environment production status        # sizes, cursors, mode, parked backlog
python3 $S --environment production scan --table cursors
python3 $S --environment production cleanup --class expired_cursors --batches 100
python3 $S --environment production probe --bytes 65536
```

1. Confirm the deployed version from `/v1/health` (`state.deployment_id`).
2. Measure before deleting: `status` lists tables, indexes and the legacy
   cursor table; `scan` reports per-column and per-index logical bytes for a
   keyset range. Cloudflare does not expose `page_count` or
   `freelist_count`; these are reported as `null`.
3. Clear expired cursors (`expired_cursors`), then the other retention classes
   (`expired_repair_metrics`, `expired_changes`, `expired_retired_objects`).
   None of these needs an export: they follow the existing retention policy.
4. If more space is needed, archive expendable telemetry first:
   `export --table repair_metrics --table demand` records a recovery manifest
   with the Durable Object point-in-time bookmark, writes digest-checked
   keyset pages to `recovery/<repo>/<id>/`, and verifies every page. Then run
   `cleanup --class repair_metrics --recovery <id>` or `--class stale_demand`.
   Cleanup refuses unverified exports and deletes only rows covered by the
   export. PR histories, resources, membership and coverage, tombstones,
   jobs, and delivery receipts are not cleanup classes.
5. Prove headroom with `probe`. It commits, waits for durability, and deletes
   rows totalling the requested size (up to 64 MiB, in 1 MiB rows); success ends degraded mode and lets the migration
   commit. Deleted rows become reusable free pages, so `database_size` need
   not shrink; report the probe result, not the file size.
6. Replay parked events (`replay`, or wait for the cron), then reconcile
   queue and dead-letter losses: `reconcile --date YYYY-MM-DD` lists retained
   raw deliveries (14 days) without receipts; add `--apply` to park them for
   replay. Messages already in the dead-letter queue have their raw bodies
   under `webhooks/<date>/`, so reconciliation covers them without consuming
   the DLQ.
7. Record a release receipt: `receipt --probe-bytes 65536` stores the
   deployment ID, ordinary legacy-client reads through
   `tauceti_worker.github_state`, storage mode, probe result and parked/held
   backlog under `~/.local/state/tauceti/github-state-recovery/`. Add the
   queue and DLQ depth from the Cloudflare dashboard. Public `/healthz` is not
   evidence of recovery.

If deletion cannot recover usable headroom, leave the service in degraded
mode: stored reads continue and ingestion stays parked. Expedite
https://github.com/TauCetiProject/TauCetiWorker/issues/316 and report the
limitation. Never call `deleteAll()` on the repository object, reset
coverage, or bootstrap from GitHub to escape the limit.

## Rollback

Earlier code cannot decode R2 cursors or replay parked pointers. Before rolling
back, set ingress to `apply`, drain the parked and held prefixes to zero, and
wait at least eleven minutes for outstanding R2 cursors to expire. Do not roll
back to code that would acknowledge queue messages without parking support
while the repository is full.

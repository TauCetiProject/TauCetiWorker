# GitHub state migration source tooling

Issue [#316](https://github.com/TauCetiProject/TauCetiWorker/issues/316) owns the
production migration. This change implements its **test-only source freeze,
complete export and source verification**. It does not implement a PostgreSQL
import, target acceptance checks, backend switch or production release receipt.
Production freeze is rejected by the service even if a caller supplies acceptance
flags. No deployment or live migration is performed by installing this code.

The prerequisites are deployed and accepted
[S0 #303](https://github.com/TauCetiProject/TauCetiWorker/issues/303) and
[S1 #308](https://github.com/TauCetiProject/TauCetiWorker/issues/308). S1's repository
backend and target contract are absent from this revision. A supplied target label
is an intent, not proof of target identity, capacity, schema or restore acceptance.
`source_verified` is explicitly not a release or permission to switch writes.

## Commands and durable state

Run from the repository root. Existing admin credentials are read privately from
`~/.config/tauceti-github-state[-test]/admin-token`. The command never prints SQL
rows, request bodies, credentials or arbitrary server error bodies.

```sh
# Read-only by default; production preflight reports blockers.
python3 services/github-state/scripts/migrate.py --environment production
python3 services/github-state/scripts/migrate.py --environment test status

# Choose one UUID v4 and reuse it on every invocation/restart.
python3 services/github-state/scripts/migrate.py --environment test \
  --generation c707ddba-076d-4a63-8696-96533031ce88 \
  --target neon-test-branch-database --apply freeze
python3 services/github-state/scripts/migrate.py --environment test \
  --generation c707ddba-076d-4a63-8696-96533031ce88 \
  --target neon-test-branch-database --apply --steps 500 resume
```

Without `--apply`, mutation commands perform preflight only. Environment selection
is mandatory. Each resume performs bounded steps and prints sanitized checkpoints;
repeat it until `source_verified`. A failure records a controlled reason code and
retains the last committed checkpoint. Retry with the same generation and target.
A deployment identity change rejects further export work and keeps the source
fenced; do not remove the marker to bypass this check.

The private R2 control record is `control/<repository ID>/migration.json`. It records
generation, source DO/deployment/schema identity, intended target, phase,
authoritative recovery bookmark, exact revision, counts, canonical hashes and
failure reason. No checkpoint is written into the frozen repository SQL database.
Only one migration command at a time runs in the source DO. The test environment
alone has `MIGRATION_ENVIRONMENT=test`; production has no enabling binding.

The fence is installed before waiting for all pre-existing repository requests
and alarms to drain. New event application, configure, repair, activity refresh,
tick, cleanup, probes and schema writes are blocked. Stored reads suppress optional
demand/metrics/projection writes, retain timestamps and support 304s and both SQL
and R2 cursors. Reads needing a legacy destructive rewrite report unavailable.
The final SQL guard rejects unexpected writes after draining. On restart the DO
loads the durable fence before any legacy schema initialization. Already-running
code that predates this protocol cannot be fenced by this test tool: production
must additionally validate deployment ownership before enabling any freeze.

Queue consumers use the migration marker to select S0's durable parking path,
without spending retries on a frozen DO. Pinned bodies and deterministic pointers
retain original delivery timestamps. Replay stays stopped until the source fence
is released. The bucket's scheduled expiry is conservatively held while any
migration is retained, including after source verification. No R2 lifecycle rule
may independently expire migration or parked data. Production integration must
also drain pre-existing retention operations before treating the pin as a barrier.

## Archive contract

Every discovered user table must match the explicit `REQUIRED_TABLES` manifest;
a missing or unexpected table stops migration. Each table records columns, SQL
types, defaults and primary-key positions. Empty tables are included. A frozen
source is synced and an authoritative DO bookmark is mandatory; observational
daily snapshots and the changes journal are never migration input.

Pages are stored under `migrations/<repository ID>/<generation>/<table>/<index>.json`.
They use rowid keyset pagination with a maximum of 250 rows and a 512 KB logical
byte target (one larger row can pass for progress). Every cell contains its SQLite
storage type and exact string value or null. Integers and signed 64-bit rowids
never cross JavaScript as numbers. Text, including JSON, is preserved verbatim;
blobs use hex and real values use SQLite's 17-digit round-trip representation.
The canonical table checksum is SHA-256 folded over these typed rows in rowid
order, independent of page size. Each R2 page also has a SHA-256 upload digest.
Checkpoints have constant size per table; they do not accumulate page lists.

Verification rereads each page and compares its typed rows against the still-frozen
source. It checks schema, revision, counts, hash continuity and explicit `$r2`
reference reachability. Bounded R2 listing inventories resource, cursor and staging
prefixes, including short list pages. Staging references can be implicit in job
keys/start timestamps/page numbers. Inventory records preserve key, size, etag and
original expiry. A retained `receipt.json` records the verified source archive.
It contains no evidence about a target that has not been imported and tested.

All jobs, leases, cooldowns, demand, incomplete coverage, tombstones, delivery
receipts, cursors, histories, head indices, survey state and activity evidence are
exported unchanged. A future importer must expire only execution leases owned by
dead workers while preserving retry/quota due times and generations. Import must
not configure the repository or enqueue/bootstrap GitHub work.

## Abandonment and remaining production gates

Before any target accepts writes, the test export can be explicitly abandoned:

```sh
python3 services/github-state/scripts/migrate.py --environment test \
  --generation c707ddba-076d-4a63-8696-96533031ce88 \
  --target neon-test-branch-database --apply abandon
```

This retains archives and the abandonment receipt and releases the SQL fence.
Existing parked-ingress mode, if separately selected, remains under its operator's
control. Normal cron resumes the existing reconciler and replay within their
existing bounds. A full source remains degraded; keep its stored reads and durable
input rather than forcing it writable. Export generations cannot be reused.

Before production enablement, S2 still needs server-verified S0/S1 acceptance,
R2/queue/DLQ ownership, Neon project/region/version/roles/storage/restore checks,
empty target validation, resumable import and target checksums/integrity, old/new
cursor and unchanged-client verification, stored-only shadow reads and a comparable
warm-read p95 within 20%. The write switch must atomically fence one authority and
be crash safe; parked/DLQ cleanup follows a committed target delivery receipt.
Record deployment/schema identities, cursor coverage, backlog/latest input, measured
latency, GitHub request rate and write owner in the production release receipt.

After PostgreSQL accepts writes, retain it and fix forward. The old DO can only
serve an explicitly timestamped emergency last-known-data fallback; it is stale
and must never become writable by flipping a routing flag. Keep it and migration
archives read-only through observation. Retirement and reverse migration require
separate deliberate procedures.

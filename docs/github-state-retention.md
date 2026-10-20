# GitHub state retention controls

The service implementation and its retention/drill runbook live in
[TauCetiTools](https://github.com/TauCetiProject/TauCetiTools/blob/main/docs/github-state-retention.md).
This repository owns the Worker-specific authenticated recovery CLI. The
service adds bounded, checkpointed SQLite/R2 cleanup, prepared capacity alarms,
optional verified CI archival, and repeatable local outage/restore drills for
[issue #317](https://github.com/TauCetiProject/TauCetiWorker/issues/317).

Use the existing test/production token directories described in
[storage recovery](github-state-recovery.md). These commands call service admin
endpoints; they do not query GitHub or print credentials:

```sh
S=services/github-state/scripts/storage_recovery.py
python3 "$S" --environment test retention
python3 "$S" --environment test retention --mode dry_run --archive-ci
python3 "$S" --environment test export --table resources --table deliveries --table demand
python3 "$S" --environment test receipt --probe-bytes 65536
```

Inspect complete dry-run scan counts, verify the export/restore receipt and
observe test monitoring before enabling new deletion or CI archival. Expect
`policy.mode: dry_run`, per-class progress, and a writable probe. A large store
requires multiple bounded passes. Each policy update replaces the complete
set of overrides:

```sh
python3 "$S" --environment test retention --mode enforce --archive-ci
python3 "$S" --environment test retention --mode enforce --disable expired_changes --pin expired_deliveries --pin daily_archives
python3 "$S" --environment test retention --mode dry_run --disable expired_changes
```

The last command disables future CI archival and the named journal cleanup
class. Other S0 expiry classes keep running unless individually disabled.
Reads and input application remain enabled. Retain pinned ingress, archives and
resource spills on rollback. Stop if parking cannot be proven durable, an
archive/restore digest fails, or a cleanup checkpoint stops advancing. Follow
the canonical service runbook for budgets, all class names, alarm ownership,
exact recovery actions and drill evidence.

The CLI can be merged before the service release, but the `retention` command
requires the service endpoint to be deployed before use. This release does not
select PostgreSQL or change the Worker dependency pin. PostgreSQL outage and
Neon restore drills follow #308's repository-store interface and isolated test
infrastructure; queue/DLQ management counts and complete spill reference
retirement remain explicit follow-up work under #317.

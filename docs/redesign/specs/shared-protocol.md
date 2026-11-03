# Shared service protocol

The first implementations use this protocol for immutable inputs, jobs, attempts, evidence, and authority. Its purpose is interoperability between independently deployable services. A team can implement one component without deploying the complete redesign.

Requirements using **must** define the v1 contract. Numerical defaults are initial configuration values and benchmark objectives; operators can change them without changing record identity.

Each service implements the subset needed by its stated first task. Agree the shared identities and command semantics used at that boundary; the complete snapshot, dependency, review, and build platform is not a prerequisite for a dispatcher pilot. The [assignment implementation handoff](work-assignment-service.md#implementation-handoff) specifies how existing GitHub source identities and worker execution fit that first increment.

## Identity and serialization

Use opaque UUIDs for `change_id`, `revision_id`, `job_id`, `attempt_id`, `review_id`, `finding_id`, `cohort_id`, and `event_id`. API callers must not infer meaning from their string representation. A `library_id` is a stable registry identifier; repository URLs and display names can change.

Use lowercase hex SHA-256 digests for service-owned content identities. Preserve Git's object-format identifier separately, such as `git:sha1:<40 hex>`. A Git SHA is not a substitute for an input digest that includes dependencies and execution policy. Times are RFC 3339 UTC. Leases use database time. Represent unbounded counters and byte quantities as decimal strings in JSON when they could exceed JavaScript's safe integer range.

Canonical digest input uses UTF-8 JSON with keys sorted lexicographically, no insignificant whitespace, integer-only numeric values, and no NaN or duplicate keys. Hash fields containing timestamps, signatures, transport URLs, and access tokens are excluded from content identity. Lists with semantic order retain it; inventories and dependency entries are explicitly sorted by identifier. Publish language-independent digest fixtures with the implementation.

Every mutable row has an integer `version`. Ownership records additionally have `authority_epoch`. Execution attempts have a monotonic `generation` per job. These identify different things: a new source revision does not automatically transfer authority or revoke a review.

## Immutable source snapshots

`POST /v1/snapshots` initiates an upload and returns an upload session with missing blob digests. The caller uploads missing bytes using scoped object-store URLs, then finalizes the inventory with `POST /v1/snapshots/{id}/finalize`. Only finalized snapshots can be executed or referenced by accepted changes.

```json
{
  "schema": "tauceti.snapshot/v1",
  "library_id": "core",
  "base": "git:sha1:008df47307b7cc5018672dec8387e5e78ef5d816",
  "files": [
    {"path": "TauCeti/Example.lean", "mode": "100644", "sha256": "<64 hex>", "size_bytes": 4120}
  ],
  "dependency_snapshot_id": "<uuid>",
  "toolchain_digest": "<64 hex>"
}
```

The example abbreviates an inventory. The finalized inventory describes the entire candidate source tree, not just the changed file. A transport may encode a base plus changed/deleted paths, but the service resolves it to the complete canonical inventory before computing the snapshot digest. Include tracked and relevant untracked/ignored source inputs. Exclude Git internals, credentials, editor state, and generated build outputs using the versioned project input policy. The build policy owns the authoritative exclusion rules.

Paths are relative POSIX paths. Reject absolute paths, `..`, duplicate normalized paths, special devices, and archive escapes. V1 accepts regular files and explicit executable modes; symlinks and submodules require explicit policy handling rather than silently dereferencing host paths. An ordinary Lake dependency is an entry in the dependency snapshot, not an unexamined local symlink.

Finalization verifies every digest and byte length. Changes after submission create another snapshot. Source bytes needed by an active obligation remain pinned. A local client rechecks the captured input inventory before reporting that diagnostics apply to the current working tree; if the tree changed, display the actual snapshot and offer to submit the newer one without discarding the result.

## Dependency snapshots

A dependency snapshot contains the complete resolved graph, exact source revisions or snapshot digests, package names, library IDs, Lean/toolchain identity, source configuration identities, and an edge list. One package identity maps to one revision. Include externally managed Mathlib and other packages. Reject unresolved diamonds and cycles in the actual package graph with the conflicting paths.

The dependency service returns immutable IDs and content digests. A new upstream head does not mutate a snapshot or cancel jobs using it. A changed remote URL does not change dependency identity if the authenticated content identity is unchanged.

## Jobs and attempts

A job describes an obligation; an attempt describes an execution. V1 job states are `ready`, `leased`, `waiting`, `succeeded`, `failed`, and `cancelled`. `waiting` carries an explicit dependency, retry time, or operator question. `failed` means the obligation's policy has declared a terminal outcome; a transient attempt failure normally leaves the job eligible for another attempt.

Attempts transition through `assigned`, `running`, `checkpointed`, and one of `succeeded`, `failed`, `expired`, or `cancelled`. Checkpoints can be published while running. Every attempt has its own logs, output references, resource use, and failure classification. Do not overwrite previous attempts.

Starting an attempt atomically checks job eligibility, capacity reservations, and authority, increments `generation`, and creates the lease. Renewals and authoritative completion must match `(job_id, attempt_id, generation, authority_epoch)`. Lease expiry is determined using database time. Heartbeat records cannot independently declare success.

Defaults for AI attempts: 60-second heartbeat, 300-second lease, renewal during long tool calls by the harness, and a 90-minute execution budget unless project policy differs. Build attempts use their own resource-aware timeout. Expiry enables reassignment; a late worker may upload historical evidence, but cannot overwrite the replacement's current state.

Distinguish loss of connectivity from an explicit cancellation or revocation. Failure to renew a central assignment lease must not by itself stop useful AI work. The [assignment outage policy](work-assignment-service.md#dispatcher-outages-and-recovery) continues active work, falls back to existing selection/claims, and reconciles results later. Generation fences still protect coordinator records from stale mutable completion; they do not discard useful outputs or make the dispatcher a prerequisite for worker execution. Build-executor resource reservations follow their own process/isolation and reconciliation requirements.

## Command API behavior

Require authenticated TLS requests for mutations. Tokens identify a human, worker, or executor and carry project/library/action scope. Return public projections separately. Keep provider credentials on contributor hosts; no service needs a worker's general-purpose GitHub or inference account token.

Build executors additionally carry cluster, capacity-owner, pool, and node/session identity under the [federated cluster contract](build-cluster.md). A short-lived enrollment token authorizes registration and is exchanged for a distinct renewable node credential; it is not a permanent build or cluster-administration token. SSH bootstrap, collaborator enrollment, and managed pool adapters use the same attempt/result semantics. A pool's capacity cannot also be counted as independently available direct-node capacity.

All POST mutations accept `Idempotency-Key`. Persist `(principal, route, key, request_digest, response)` with the mutation. The same key and body returns the original result; a different body returns `409 idempotency_conflict`. Retain command records for at least 30 days initially; clients must not retry older commands blindly after expiry. Keep durable uniqueness constraints on permanent domain identities independently of command-record retention.

Use `If-Match` with the resource version for replacing mutable intent, configuration, or source refs. Append-only evidence generally needs no current-revision precondition: the service records its actual input binding. Automatic projections may be rebuilt without altering evidence.

| Response | Meaning |
| --- | --- |
| `200` / `201` | Command durably applied or idempotently replayed |
| `202` | Durable asynchronous obligation created, with its resource URL |
| `204` | No eligible assignment at present; optional retry hint |
| `409` | Version, generation, ownership, or idempotency conflict; typed current state |
| `422` | Invalid snapshot, dependency graph, policy, or domain request |
| `429` | Principal quota exhausted, with `Retry-After` |
| `503` | Service unavailable; a caller must not interpret this as an empty queue |

Errors have `code`, `message`, `request_id`, and `retryable`, plus sanitized domain details. Never return credentials or arbitrary executor environment variables. Collection reads use opaque bounded cursors and stable snapshot semantics. Initial JSON request limit is 1 MiB; bulk source, diagnostics, and logs use referenced objects or chunked streams.

## Evidence and permissive review

Evidence records identify their producing principal, actual source snapshot/revision, dependency snapshot, policy, execution identity, and payload digest. Preserve the original record when a derived projection reuses it. A carried approval names its original review and reason only when a new explicit judgment is made; an ordinary edit does not need an extra AI statement to keep approvals effective.

Build receipts state exact requested and executed targets/phases, success/failure/incomplete result, input identity, trusted executor identity, artifact manifests, log references, and resource usage. An incomplete job cannot become a successful receipt by timeout or missing callback.

Review applicability is semantic state governed by [review continuity](review-continuity.md), not automatic equality of source digests. A stale source pointer makes a review historical evidence; it does not mechanically make its judgment ineffective. Explicit revocation or a scoped blocking finding changes effective review state.

## Events and subscriptions

An authoritative transaction updates domain state and inserts an outbox event containing `event_id`, entity ID, entity version, authority epoch, type, timestamp, and bounded references. An outbox relay retries delivery until acknowledged. Consumers deduplicate by event ID and validate relevant versions when acting.

Messages contain pointers and compact routing data, not large logs. Payload upload completes before the database publishes its reference. Failed transactions leave unreferenced objects for later garbage collection. Consumers never infer a missing collection from a failed object download.

Subscriptions use SSE or bounded long polling with a resume cursor. A lost connection does not lose an assignment. Events are notifications to inspect state; the job table remains authoritative. Coalesce repetitive source and dependency notifications. Logs use per-attempt sequence numbers so clients can resume without requiring a global log sequencer.

## Authority during migration

Each work key has one owner: `legacy`, `shadow`, or a named active service, plus an epoch. `shadow` may propose but never publish competing mutations. Importing state does not itself transfer ownership.

To transfer, record the old owner's final known state and any live attempt, establish the new epoch, and configure both mutation paths to check ownership. Existing paid attempts can finish under a documented handoff or upload evidence for adoption. A legacy publisher unable to check ownership must be excluded from the selected work keys through its supported configuration before activation. Do not claim atomic authority transfer across an unmodified external publisher.

Assignment routing includes the documented fallback policy. A central-ownership record does not disable legacy selection indefinitely while the dispatcher is unreachable. Normal authority transfer, offline useful work, and later reconciliation are distinct operations; a service outage must not require a fleet-wide stop or an operator-mediated rollback before workers can continue.

For builds, ownership can be switched per candidate/workload class without changing who owns review or merge. For assignments, one authoritative dispatcher serves all participating workers for a key. For Git refs, only the designated publisher writes. Rollback issues a new epoch; never restore an old epoch or erase imported work.

## Storage and recovery

Use foreign keys or checked reference publication for SQL identities, uniqueness constraints for work keys and external IDs, and immutable object manifests. Initial tables include `principals`, `libraries`, `dependency_snapshots`, `snapshots`, `jobs`, `attempts`, `leases`, `evidence`, `authority`, `commands`, `events`, and `outbox`. Component-specific tables extend this set.

Provide migrations, schema-only test isolation, database backups, referenced-object export, and a restore exercise. Garbage collection first marks unreferenced objects, waits seven days initially, then rechecks references before deletion. Active jobs, accepted staging obligations, and published release manifests pin their required inputs. Use object-store versioning or equivalent retained manifests where supported by the chosen storage design; do not assume a database backup alone restores source blobs.

Implementations must publish digest fixtures, request/response examples, and migration/restore commands. A pilot can use one PostgreSQL database and one bucket with separate namespaces. Services must work without the native web interface.

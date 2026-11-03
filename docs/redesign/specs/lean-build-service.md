# Lean build service specification

Build a service that executes Lean checks on dedicated machines for both agents and CI. The first deployment must accept dirty source snapshots, preserve warm dependencies, stream diagnostics, and return exact-input receipts without occupying an agent machine's build resources.

The implementation is greenfield. Learn from Kiln's behavior and measurements without requiring its code, cluster, infrastructure tools, or operational workflow. The [federated build-cluster specification](build-cluster.md) is part of this component's contract: machines owned by different people join through SSH bootstrap, collaborator token enrollment, or managed pools such as Kubernetes. One logical cluster can combine all three across independent networks.

The [shared protocol](shared-protocol.md) defines identities, snapshots, jobs, commands, and evidence. The service can deploy before central AI assignment: its own build-job store and executor admission implement that subset directly, and later share it with the common coordinator.

## First deployable scope

Provide a client integrated with the existing worker verifier, a Cloudflare-hosted API, PostgreSQL build-job records, R2 payload storage, and a portable executor protocol with the three cluster assembly adapters. Initial fixture qualification can use one direct executor while adapters are developed in parallel. Run existing project checks inside isolated candidate workspaces. Whole-project execution is sufficient for v1; do not require a new Lake scheduler or a common Kubernetes cluster.

V1 supports `interactive`, `candidate`, `staging`, and `dependency` classes. Interactive requests can select modules or probes; candidate and staging requests select a named server-owned verification policy. Output must identify which checks actually ran. Remote REPL sessions, per-module distributed execution, generalized arbitrary shell jobs, and automatic purchases of compute are later extensions.

Use TypeScript for the Worker API and a Python adapter for the existing harness. The executor and bootstrap distribution must not assume a preinstalled interpreter or container runtime; a standalone Go agent is the initial proposal. Keep the protocol language-independent. Put reusable implementation in TauCetiTools or a dedicated build-service repository, with only the worker adapter here; generated project script copies are not independent implementation locations.

## User behavior

Proposed commands are:

```text
tauceti build submit --worktree PATH --class interactive --target MODULE --json
tauceti build submit --worktree PATH --class candidate --policy POLICY --json
tauceti build wait JOB --events --json
tauceti build result JOB --json
tauceti build cancel JOB
```

The client captures a reproducible source snapshot including relevant uncommitted files, uploads missing content, and submits without first pushing a branch. It renders paths and line/column diagnostics relative to that snapshot and the caller's worktree. A changed local file is marked as newer than the diagnostic; the old result remains available.

The existing verification helper receives an explicit backend option `local|remote|auto`. Default local behavior remains until a selected pilot enables remote execution. `auto` may fall back locally only when configured host resource admission permits it, and records why; it must not launch remote and local copies simply because a response is slow. CI uses an explicit remote job, not an implicit local fallback on an agent host.

## Submission API

After snapshot finalization:

```http
POST /v1/builds
Idempotency-Key: <uuid>
```

```json
{
  "snapshot_id": "<uuid>",
  "dependency_snapshot_id": "<uuid>",
  "class": "candidate",
  "targets": ["default"],
  "verification_policy_id": "core-candidate-v1",
  "consumer": {"kind": "github_pr", "repository": "TauCetiProject/TauCeti", "number": 123},
  "deadline_at": null,
  "resource_hint": {"cpu": 4, "memory_mib": 16384}
}
```

The server resolves a policy version to an immutable policy digest and records it in the job. Requested targets must be permitted by that policy; clients cannot remove required phases. Resource hints guide placement and can be raised by server policy. The caller cannot elevate itself into an operator-only priority class.

The first candidate policy wraps today's whole-library build, local axiom diagnostics, module-system checks, changed-module lint, and shim checks with their current scope semantics. The authoritative CI policy additionally includes the existing transitive axiom/global ownership and full-CI requirements as configured for that library. Reuse the actual runners and inventories; do not replace a transitive check with a local diagnostic while retaining the same policy name. Record policy capabilities explicitly so consumers can request missing phases instead of duplicating completed ones.

The response is `202` with `job_id`, `subscription_id`, `input_digest`, `state`, and URLs for events and result. Equivalent existing work returns its job with a new subscription. Existing success can return `200` and its reusable receipt. Caller-specific deadlines and display labels do not split equivalent execution keys; incompatible trust requirements do.

| Endpoint | Behavior |
| --- | --- |
| `GET /v1/builds/{id}` | State, queue reason, placement, targets, attempts, and receipt references |
| `GET /v1/builds/{id}/events?after=...` | SSE or bounded pages of diagnostics and lifecycle events |
| `GET /v1/builds/{id}/result` | Structured result, receipt, output manifests, and logs |
| `DELETE /v1/builds/{id}/subscriptions/{subscription_id}` | Remove this caller's interest; cancel only when policy permits and no consumer remains |
| `POST /v1/enrollments/redeem` | Bind a scoped enrollment capability to a generated node identity; cluster-spec registration lifecycle |
| `POST /v1/executors/{id}/sessions` | Reconnect an enrolled direct executor or managed pool endpoint and reconcile live work |
| `POST /v1/executors/{id}/claim` | Atomically reserve resources and lease one matching build |
| `POST /v1/build-attempts/{id}/heartbeat` | Renew lease and report bounded resource telemetry |
| `POST /v1/build-attempts/{id}/complete` | Publish checked result references for the current attempt |

Enrollment credentials are issued with cluster, owner/pool, workload, and capacity scope. Collaborators redeem a token through the one-line installer without granting the coordinator SSH access to their machines. SSH bootstrap uses the same installer and registration protocol; managed pools use the same execution contract. Ordinary build clients cannot impersonate executors or expand their enrollment scope.

## Input and execution identity

The execution key is a SHA-256 digest over source snapshot, resolved dependencies, toolchain distribution and platform, build configuration, relevant environment, verification implementation/policy digest, and requested target/phase set. The server resolves aliases before computing the key. Store the complete inventory once and reference it from receipts.

Warm workspace identity is separate and can be coarser: toolchain, dependency graph, platform, and compatible build configuration. Reusing a warm workspace must not reuse mutable candidate outputs without validating their input keys. Use read-only dependency layers and private writable candidate outputs. Preserve toolchain ABI and platform distinctions.

Initially reuse the existing verification input/policy machinery for exact-input compiled evidence, while allowing broad semantic review continuity. Do not let the coarse source fingerprint force a full physical rebuild when Lake can incrementally establish the new outputs. Inspect current Lake cache support and the hosted cache's artifact manifests before adding another cache implementation.

Dependencies must be retrievable from internal immutable source/artifact endpoints. The executor may hydrate a trusted upstream mirror during preparation, but a successful warm build must not require repeated GitHub metadata requests. Report preparation separately from execution.

## Executor lifecycle

1. Reconcile the executor/pool's owner caps and active reservations, then claim a job and reserve CPU, memory, and scratch disk transactionally. The local arbiter confirms current capacity before execution.
2. Fetch immutable source and dependency manifests; verify payloads.
3. Create a private candidate workspace over warm read-only dependencies and a writable local scratch layer.
4. Execute the server-selected policy runner with scoped resource limits and no publication credentials.
5. Stream diagnostics and persist resumable logs and checkpoints.
6. Validate final input identity, collect exit codes and output manifests outside the candidate's writable namespace, and upload results.
7. Complete the current attempt using its generation and authority epoch; release resources even if result delivery needs replay.

Heartbeats run independently of the checking subprocess. Executors report actual free resources, running attempts, and toolchain availability. The service reconciles abandoned reservations with leases; an executor must not immediately reuse resources occupied by a still-running orphan process. Stop or account for that process before admitting another job into the same capacity.

Direct executors use outbound HTTPS, so collaborators behind NAT need no incoming port, VPN, shared filesystem, or Kubernetes membership. Managed pool adapters map attempts to their own runtime. Enrollment, runtime isolation, owner policy, and artifact trust are separate capabilities; the scheduling/result protocol cannot assume a particular host-management mechanism.

Use operating-system isolation appropriate for untrusted Lean elaboration and native build steps. Candidate code cannot access other candidates' writable caches, signing keys, control-plane credentials, or host services. A compromised candidate cannot choose the verification runner or sign its own receipt. Long-lived warm artifacts remain read-only to candidate execution.

## Scheduling and performance

Separate interactive, staging/operator, candidate, and dependency queues with configurable capacity floors and borrowing. Initial suggested executor capacity split is 20% interactive, 30% staging/operator, and the remainder shared by candidate/dependency work; this is a pilot setting to tune from measured demand. Unused reservations are borrowable. Long borrowed jobs may occupy a floor until a safe checkpoint, so report the delay instead of claiming immediate preemption.

Schedule by CPU, memory, scratch space, platform, toolchain, warm locality, measured machine speed, transfer cost, owner availability, and owner caps. Use aging to prevent starvation. Interactive checks should not sit behind a large dependency rebuild on every executor. Give expensive builds explicit resource classes and retain their measured peak memory for future placement. Compute, export, and retained disk have distinct accounting so artifact cleanup need not indefinitely hold otherwise usable compilation capacity.

Initial benchmark objectives:

- 100 build submissions/minute with 1,000 concurrent subscribers and 10,000 pending jobs; bounded API command p95 under one second at the API boundary in the benchmark region, excluding payload uploads.
- New diagnostics visible within two seconds of server receipt; retained logs remain available after reconnect.
- At most one ordinary active attempt per execution key; duplicate subscribers do not multiply builds.
- Report interactive end-to-end p50/p95 for representative warm edits and compare with local execution. No universal compilation-time promise is made before workload measurement.
- Expose staging wait caused by memory saturation independently of CPU saturation.

These are targets for load tests, not achieved capacities. The pilot's expansion criterion is improved natural-work throughput and acceptable interactive latency without increasing duplicate builds or starving existing CI.

## Results and receipts

```json
{
  "schema": "tauceti.build-receipt/v1",
  "job_id": "<uuid>",
  "attempt_id": "<uuid>",
  "input_digest": "<64 hex>",
  "snapshot_id": "<uuid>",
  "dependency_snapshot_id": "<uuid>",
  "policy_digest": "<64 hex>",
  "status": "succeeded",
  "phases": [{"name": "build", "status": "succeeded", "exit_code": 0}],
  "artifacts_manifest": "<sha256 object reference>",
  "logs_manifest": "<sha256 object reference>",
  "executor_identity": "<registered executor>",
  "resource_use": {"cpu_ms": 120000, "peak_memory_bytes": "8589934592"}
}
```

The abbreviated phase list must contain every requested phase in actual receipts, with omitted work explicit. `incomplete` is distinct from a failing check. Classify incomplete attempts as infrastructure, executor loss, OOM, deadline, cancellation, source invalidity, or unavailable dependency. Preserve the checking command, sanitized environment identity, policy digest, original exits, and provenance of reused phases.

Candidate and CI consumers can reuse the same successful execution only if input, targets, policy, and trusted-origin requirements match. A narrower interactive success does not certify full CI. If the service issued a valid receipt before the consumer disconnected, later retrieval must not rerun the build.

A stale attempt uploads outputs as historical evidence. Matching immutable outputs from an accepted executor may be adopted through a new reconciliation command after validation; generation loss alone does not invalidate their mathematical work. They cannot complete a replacement attempt by bypassing its ownership checks.

## Existing GitHub integration

A trusted adapter submits the immutable PR head and dependency vector, subscribes to completion, and publishes one status for the appropriate GitHub SHA. On publication, the adapter verifies the receipt and the SHA mapping. It may publish an old head's actual result without claiming that result applies to a newer head.

Persist a status-publication outbox and reconcile failed callbacks. GitHub delivery failure does not rerun Lean. Use webhook or shared-state observations to coalesce status work. The native system later consumes the identical receipt directly.

The first pilot compares remote and existing verifier outputs on a bounded set of naturally selected candidates. Diagnostic comparison can run on already available receipts before authorizing duplicate executions. Do not launch paid agent sessions or extra fleet capacity merely to obtain rollout measurements.

## Failure and cancellation

| Failure | Required behavior |
| --- | --- |
| Client disconnect | Job continues while another subscription or durable CI obligation needs it |
| Duplicate submit | Return the existing job/result and retain all consumer associations |
| Executor dies | Preserve logs/checkpoints, expire lease, release reconciled resources, retry as infrastructure failure |
| OOM | Record observed peak/limit and reschedule in a larger authorized class; avoid unbounded identical retries |
| Source changes locally | Keep result attached to captured snapshot; offer a new submission |
| Object upload succeeds but SQL fails | Object remains unreferenced; retry command, then garbage collect if never referenced |
| Completion callback is lost | Replay the same completion command/result, never rebuild solely for callback recovery |
| One subscriber cancels | Other subscribers and staging obligations retain the shared job |
| Candidate reports success itself | Treat as untrusted diagnostics, not an authoritative receipt |

Three repeated infrastructure failures initially put the job in `waiting` with an operator-visible reason and retained obligation. The operator or a repair agent can change placement or resume; a retry budget is not permission to discard an accepted staging contribution.

## Deliverables and acceptance

Deliver a service implementation, versioned migrations, client/harness adapter, portable executor, SSH bootstrap, collaborator token/installer workflow, managed pool adapter, CI status adapter, sandbox configuration, telemetry dashboard, and install/rollback/restore runbook. Document exactly which existing verifier phases and cache formats are reused. Record relevant lessons from Kiln without making compatibility with or adoption of its implementation a deliverable.

Acceptance scenarios must demonstrate:

1. An uncommitted new Lean file is checked remotely and diagnostics point to its captured lines.
2. Two candidates share dependencies while neither can modify the other's outputs; a warm edit avoids repeated dependency hydration.
3. Ten identical submissions result in one execution and ten durable subscriptions.
4. Agent and CI consumers reuse a matching authoritative receipt; a phase or policy mismatch requests the missing work.
5. Executor death, lost callbacks, API retry, and restart preserve job identity and outputs.
6. A lease-losing executor cannot overwrite current state, but its useful uploaded artifact can be adopted explicitly.
7. A library's pinned build continues after an upstream head advances.
8. A staging job obtains its reserved capacity under sustained candidate load, with actual queue delay measured.
9. Database and object-reference restoration recovers a waiting build and its source without GitHub availability.
10. Live enable/disable of the remote backend affects new selected jobs without terminating existing rounds or pausing admissions.
11. The same logical build cluster runs requests across independently owned SSH-bootstrapped machines, token-enrolled machines behind NAT, and a managed pool, with equivalent result semantics and enforced owner caps.

Pilot on selected workers and one CI workload after ordinary implementation checks pass. Broaden by workload/library based on latency, resource use, and useful completions. Rollback routes new work to its previous supported backend and leaves remote jobs and receipts retrievable until their consumers finish.

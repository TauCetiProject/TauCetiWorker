# Efficient worker resource accounting

Replace resource telemetry's host-wide process scans with inexpensive readings from the existing worker cgroup and the round's kernel CPU accounting. Telemetry failures must never delay work, affect admission, or require operator intervention.

## Evidence and scope

`resources.py` independently enumerates `/proc` every two seconds for each active round. A Tauchonki scan of roughly 19,000 entries cost 0.45 CPU-seconds; most entries were unrelated idle kernel threads. Live statistics are published only once a minute.

A managed worker's scope contains its wrapper, loop, successive rounds, and independently running background verification jobs. Scope counters therefore measure more than the round's process tree. Preserve this distinction rather than silently changing existing round statistics. Keep containment, memory policy, capacity, and cleanup safety unchanged.

## Implementation

1. **Use the existing scope identity.** Resolve the current cgroup with `worker_scopes.cgroup()`. Select scope measurements only when its name starts with `worker_scopes.prefix(workers_runtime_dir(), cfg.wid)` and ends in `.scope`. Otherwise continue with round-only accounting. Use the existing environment/runtime identity, without status-file dependencies, process discovery, D-Bus calls, or new scopes.

2. **Preserve round CPU accounting.** Keep the current start/end `getrusage()` difference for completed-round `cpu_seconds` and cumulative completed-round worker totals. This retains accounting for the round and its reaped descendants without including sibling background verification. Preserve round IDs, timestamps, monotonic elapsed time, and completed-round accumulation. A live `getrusage()` value excludes unreaped children: either label it as partial or omit it; do not present it as complete live CPU usage.

3. **Publish scope readings separately.** Add a distinct `worker_scope` object identifying the scope and observation time. Read cumulative CPU from `cpu.stat`, anonymous/cache/charged memory from `memory.stat` and `memory.current`, the scope-lifetime charged-memory peak from `memory.peak`, task count from `pids.current`, and optional I/O/pressure counters. Tauchonki exposes the principal counters but currently lacks `io.stat`; absence of that file is acceptable. These counters include descendants. [Kernel cgroup interfaces](https://www.kernel.org/doc/html/latest/admin-guide/cgroup-v2.html).

   Describe the scope as including background verification and wrapper activity. Keep its cumulative CPU separate from sums of round CPU. Show anonymous memory alongside charged memory and cache; charged memory and its peak are not RSS or an exclusive share of shared artifact memory. Task count includes threads. Leave the scope-lifetime peak explicitly labeled: there is no need to reset it, write cgroup files, detect per-descriptor reset support, or claim a per-round aggregate memory peak.

4. **Sample only for live visibility.** Retain the existing resource thread with its interval equal to the existing publication interval: 60 seconds. Read at round start and finish as well. Replace process enumeration with the small set of scope-file reads; add no separate sampling loop, event watcher, or service. Kernel CPU totals and peak memory remain available even when a short-lived child exits between refreshes. Final accounting does not depend on receiving an intermediate refresh.

5. **Remove the expensive fallback.** Delete telemetry's `process_table()` and `process_tree()` and their obsolete tests. Standalone/non-Linux workers, failed isolation, and missing or unreadable scope counters retain inexpensive round `getrusage()` accounting and its explicitly labeled process high-water figure. Missing scope fields stay unavailable individually. Catch telemetry errors and continue existing finalization; never wait or retry until metrics become available. Do not enable controllers or request privileges to obtain optional counters.

6. **Keep the data change small.** Retain existing completed-round CPU fields. Stop generating sampled tree-RSS fields for new records; add explicitly named scope memory fields instead. Update `docs/workers.md` to explain the measurements and refresh interval. Current repository consumers do not interpret the obsolete resource fields, so no history migration or compatibility framework is needed. Existing resource logs remain readable as historical records.

## Validation and deployment

Adapt the existing resource test: verify child CPU remains counted after exit, successive completed rounds accumulate once, and missing cgroup counters leave work and round CPU accounting unaffected. Check that concurrent background verification appears only in the separately labeled scope readings. Reuse small fixtures; removing the scanner removes the need to instrument or benchmark process enumeration in tests.

Run one Linux scope check with a short-lived child to confirm the kernel retains CPU and peak memory after exit without an intermediate refresh. Compare telemetry overhead once. Ship through the existing worker/runtime pin flow and confirm normal rounds finish and live readings refresh at most once a minute.

Completion means telemetry cost is independent of unrelated host process counts, measurement scopes are explicit, and no new admission conditions, limits, services, or recovery dependencies have been introduced.

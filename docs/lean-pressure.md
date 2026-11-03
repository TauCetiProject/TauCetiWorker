# Lean process pressure gate

Linux `tauceti` runs export `LAKE_OVERRIDE_LEAN=true` and set `LEAN` to the
bundled `scripts/tauceti-lean`. Bubble rounds stage the same standalone helper
inside the container. These settings reach cache preparation, local verification,
and agent subprocesses.

Before starting a compiler, the helper samples aggregate CPU use for a random
0.2–0.5 seconds. It waits while CPU use exceeds **90%**, or memory use exceeds
the threshold below, sleeping for a fresh random **1–5 seconds** before checking
again. CPU use is busy `/proc/stat` ticks, excluding idle and I/O wait, normalized
across all CPUs. Memory use is `MemTotal - MemAvailable`, also bounded by finite
cgroup-v2 ancestor `memory.max` and `memory.high` limits; clean file cache is reclaimable, while shared memory,
dirty pages, and writeback pages remain charged.

The memory threshold is
`max(60, floor(100 - 200 * WORST_FILE_RSS_BYTES / total_bytes) - 1)` percent.
This reserves strictly more than twice the measured worst file's RSS, with a
60% floor on smaller machines. Where the floor applies, twice-file headroom
cannot be guaranteed. The fixed RSS constant is recorded with its measurement
date in the helper; [measurement evidence](evidence/lean-peak-rss-2026-11-03.json)
identifies the source revision, toolchain, command, coverage, and worst module.
The helper does not continuously recalibrate this value.

Lake uses `LEAN` to discover a prefix, then executes `<prefix>/bin/lean`.
The helper therefore returns a cached toolchain overlay containing the wrapper
and links to the original toolchain's libraries and tools. Prefix, version, help,
and hash probes bypass pressure waits. After waiting, `exec` preserves arguments,
stdin/stdout/stderr, process identity, signals, and the compiler's exit status.
Missing or invalid pressure observations prevent a compilation with a clear error.
Normal waits are silent so `lake build --iofail` continues to work; set
`TAUCETI_LEAN_PRESSURE_DEBUG=1` for wait diagnostics during manual investigation.

When a Fleet timing collector is available, the helper reports pressure wait
reasons and the transition to execution over a local Unix socket. The socket
survives `exec`; its closure measures compiler elapsed time without adding a
parent process or changing signals, output, arguments, or exit status. Collection
is optional: a missing, full, or failed observer never blocks compilation.
The normal initial CPU measurement is recorded separately from time blocked on
capacity. Metadata probes are excluded. Fleet reports summed capacity-wait time
divided by summed capacity-wait plus execution time, including active waits;
parallel invocations contribute separate elapsed seconds. Wait quantiles use
bounded histograms of admitted invocations that actually waited. Existing
processes are reported separately until they acquire instrumentation.

Worker toolchains are left untouched. The worker's PATH alias catches direct
`lean` commands, and the toolchain overlay catches Lake's compiler invocations.

TauCetiFleet vendors the standalone helper and installs it, its environment, and
the Bash startup hook into each new CI guest. The hook wraps only the disposable
watchdog's `bin/lean` entry point after it has been prepared. Capacity waits
precede the watchdog's execution deadline, while its `lean-real` symlink still
points at the official compiler. It leaves official compiler files and their
snapshot manifests untouched. This catches compilation inside sandboxes that
clear their environment without invalidating the official toolchain cache.
The hook is idempotent, guards against recursive shell startup, and warns on
installation failure while allowing final CI status reporting to run. It does
not need a new base image.
Workers and CI may both use free RAM up to a common parent slice ceiling, which
preserves OS headroom; workers no longer subtract the full configured CI RAM from
their own limit. A higher worker OOM score biases emergency kernel victim
selection toward workers, without guaranteeing protection for large guests.
Existing CPU priorities remain in effect. Running disposable
guests acquire the new setup when they finish and are replaced.

This is a start-time gate with independent random retries. Existing compilers
continue running and several processes may pass a check together. It reduces
contention but does not reserve memory or guarantee freedom from OOM failures.

# Native Lake cache sharing

Compatible Lake executables can use a managed machine-wide artifact store while
older executables retain their worker-private `LAKE_CACHE_DIR`. Sharing uses
Lake's native artifacts, hashes, output mappings and invalidation. It does not
change the Lean compiler, project toolchain pins, worker counts or admissions.

The [native implementation](https://github.com/leanprover/lean4/pull/16301)
must provide atomic artifact/metadata publication,
unique download temporaries for both batch and lazy fetches, and recovery when
an artifact disappears during restore or unpack. A PATH launcher alone cannot
provide those guarantees.

## Configuration

Workers keep their usual private cache as the fallback. They also offer the
native opt-in root through `LAKE_SHARED_CACHE_ROOT`, with
`LAKE_SHARED_CACHE_FALLBACK` recording the original private path. Changing
`LAKE_CACHE_DIR` explicitly for a command overrides that opt-in. An unfixed Lake
ignores both new variables and uses the private path.

The default managed root is `~/.cache/tauceti/lake-native`. Override it with
`TAUCETI_MACHINE_LAKE_CACHE_ROOT`; set `TAUCETI_MACHINE_LAKE_CACHE=0` to keep
ordinary private behavior. Explicit operator cache overrides are preserved.
Compatible executables separate the store by toolchain identity and require
restored artifacts and working hard links on the checkout filesystem. Other
invocations use private storage. Forwarding sysroots, including the host Lean
throttling wrapper, reuse the installed release’s namespace when their Lean
libraries resolve to that release. The compiler wrapper remains in use.

Run `lake --shared-cache-dir` in a workspace to see the native selection before
loading its configuration. An empty result means private mode. Older Lake
versions do not recognize this query; workers treat that as private mode.
Capability detection shares the existing cache-preparation deadline; an
unavailable or stalled query retains private behavior.
Package-level overrides that require non-restored paths keep that workspace
private even if the initial selection query finds a compatible store.

The cooperating local store can contain workers' locally built outputs. It is
separate from the CI-only publication pool. Isolated verification and trusted
Mathlib snapshot publication do not inherit the local-store opt-in. Verification
keeps its existing handling of build outputs copied with a snapshot; this does
not certify every snapshot output as CI-produced. Bubble
retains its existing isolated caches. Shared hard links do not isolate a
same-user process that deliberately changes permissions and edits their bytes.

## Installing a matching standalone Lake

The worker also supports an isolated standalone runtime for toolchains whose
distributed Lake does not yet have the native fixes. Build the matching Lake
source with the native patch, then install it using the project interpreter.
The [v4.36.0-rc2 backport](https://github.com/kim-em/lean4/commit/32e42ebe30ab58b6c4d3afac46222c9dac66eac3)
is built from its `tests/lake` directory with
`ELAN_TOOLCHAIN=leanprover/lean4:v4.36.0-rc2 lake build Lake lake`.
Its executable is `tests/lake/.lake/build/bin/lake`, and the matching source is
`src/lake`. Install those paths as follows:

```sh
python scripts/install-machine-lake \
  --toolchain leanprover/lean4:v4.36.0-rc2 \
  --lake /path/to/build/.lake/build/bin/lake \
  --source /path/to/matching/src/lake
```

Installation checks the version, copies the standalone executable and its
library into `~/.local/share/tauceti/native-lake`, and registers the original
toolchain executable's identity. It does not overwrite files in elan's
toolchains. `TAUCETI_MACHINE_LAKE_RUNTIME_ROOT` can select another installation
root for this worker integration.

When a registered runtime exists, the worker prepends its launcher directory to
PATH. Each invocation resolves the current elan toolchain and uses only its
registered matching runtime. A missing runtime, changed original executable,
malformed registration, or execution failure before startup falls back to the
original Lake. The launcher never retries a command that has already started.
Direct calls to an unpatched toolchain binary continue using private storage.

## Optional cleanup

The existing `tauceti cleanup` background entry point collects this store using
only explicitly selected retention settings:

| Variable | Meaning | Default |
| --- | --- | --- |
| `TAUCETI_MACHINE_LAKE_CACHE_MAX_GIB` | Retained-size reclamation target across the managed toolchain directories. | Unset |
| `TAUCETI_MACHINE_LAKE_CACHE_MAX_AGE_HOURS` | Eligibility age based on filesystem change time. | Unset |

Either setting may be used independently; zero is allowed. There is no inherited
25% disk cap, implicit 72-hour expiry, worker flush, or new admission restriction
for this store. Without either setting the new collector does not perform age
or size eviction. Existing host disk-pressure behavior still applies.
Abandoned staging is checked even when neither retention setting is selected.

Set the variables in the environment when installing the existing cleanup
timer so that it retains them, or supply them to an explicit cleanup invocation.
`tauceti cleanup --dry-run` reports eligibility without deleting data. Invalid
optional settings are reported and leave the store untouched.

Cleanup inventories native artifacts, output/revision metadata, and staging.
Files with workspace hard-link aliases are retained. Completed files with one
remaining link can be removed toward the selected target; native misses recover
through refetch or rebuild. Abandoned staging is removed only after its recorded
process owner has exited. No timing grace period substitutes for race handling.

Change time approximates recent link activity, not exact last use. Active links,
live staging, and concurrent writes can keep the store above its reclamation
target. Cleanup reports excess retained data and does not terminate work to
enforce a number. Existing source-preservation rules continue to govern retained
and recovery checkouts.

Accounting separates retained allocated bytes, bytes with additional links,
removed names, and observed host free-space changes. Hard-linked files count
once within inventory. XFS reflinks and concurrent activity prevent directory
sizes or free-space deltas from proving exact physical savings.

## Switching and rollback

Enable the integration at ordinary worker round boundaries with normal worker
and CI admissions enabled. Native builds populate the store from existing
up-to-date outputs and downloads; there is no required bulk migration. Singly
linked private generated outputs can join canonical artifacts atomically.
Existing aliases retire through their current cleanup paths.

Disable the opt-in to return subsequent invocations to private behavior. Keep
the shared pool intact for any current readers. No source, credentials, Git
history or recovery receipts need to be restored to roll back cache selection.

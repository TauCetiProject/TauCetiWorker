# Local verification receipts and host capacity

`tauceti verify [--stage build|axioms|module-system|lint|shim-expiry|all]
[--base SHA] [--json] [--no-reuse] [--lint-modules FILE]` runs synchronously in a TauCeti checkout. It exits zero
only after every requested phase succeeds against unchanged inputs. Progress, original tool output
and actual exits go to stderr; `--json` writes one `tauceti-verification/v1` object on stdout.
Prompts invoke the installed absolute `scripts/tauceti-verify` wrapper with the worker's Python
interpreter. The wrapper also honors `TAUCETI_VERIFY_PYTHON` and supports Bubble's staged layout.
Wheel entry points restore its execute permission, so login-shell PATH changes cannot hide it. Existing `tauceti work`, module and checkout entry
points keep working.

The worker pins `TAUCETI_VERIFY_BASE` from the existing canonical-main checkout preparation,
`TAUCETI_VERIFY_POLICY` from surveyed labels/branch, `TAUCETI_VERIFY_STATE` to worker state, and
`TAUCETI_ROUND_DEADLINE` to the round's deadline. Standalone invocations resolve canonical main
from local Git refs only. No scope or verification step requests GitHub data or fetches refs.
The Python scope contract in `verification_scope.py` is tested against #305's V0 recipe: merge-base
diff, both rename ends, staged/dirty/deleted/untracked sources, ignored sources under TauCeti,
full audits for broad policy/pins/deletions/unrepresentable modules, and unknown scope on missing
history. Missing canonical main fails closed with an incomplete receipt: there is no resolved base to
bind evidence to. Unknown lint scope remains incomplete instead of silently establishing lint evidence.
A stage-only build or audit does not claim the skipped requested lint succeeded.

All stages run the whole-library `lake build --iofail`, the axiom/duplicates auditor (scoped only
when safe), module-system, source style lint, changed-module environment lint, and the shim ratchet.
A stage-only call runs that stage; it does not assert that prerequisites or other stages passed.
The shim checker receives merge-base source and registry snapshots, strict `--fail-on-available`,
and `--only-new` for unchanged pins outside bump policy. Older source revisions without the checker
record an empty shim phase. CI still owns whole-library environment lint. For failures in unchanged
modules, `--lint-modules /absolute/list` adds validated existing dotted TauCeti module names to the
local lint list; its contents are fingerprinted. Use the same option for final verification. Write this
list and any extra diagnostic logs outside the checkout; untracked checkout files are inputs.

Each invocation atomically writes `state/<worker>/verification/<id>/receipt.json` before launching
anything and updates it through completion. File and directory fsync precede publication. The receipt
records requested phases, scope, start/end timestamps, original audit timing lines, tool exits,
containment capability and full log paths, including the auditor's `AUDIT_PHASES_FILE` timestamps. Reused phases refer to their original receipt.
Receipts and logs are retained on failure or interruption. SIGKILL leaves the last durable receipt
incomplete, even if a tool appeared to finish. No background completion is assumed.

Evidence is reusable by phase only after a successful, input-stable invocation with the same digest.
The digest covers the full Git source inventory and actual bytes (including missing, dirty, renamed,
untracked and ignored TauCeti sources), base SHA, dependency sources, configuration, environment
value hashes, installed Lean distribution, executable hashes and versions, Python runtime, and worker
verifier code/policy. Sensitive environment values are not written. Changes conservatively invalidate
all phases. Complete inventories are stored once under `verification-inputs/<digest>.json`, referenced
from receipts. A phase index avoids scanning old receipts. Per-file digests are cached by path,
device, inode, size, mtime and ctime; any metadata change rereads the bytes. The metadata cache is
bounded. Receipts and logs remain retained; there is no automatic deletion of incomplete evidence.
Temporary shim archives are removed after use. Source metadata sampling every two seconds catches mutations restored to their original bytes; a final full digest
check rejects changed inputs. Toolchain/dependency metadata is checked at the final boundary rather
than repeatedly scanning large installations while queued. Environment hashing is limited to build
configuration and tool-search variables; provider credentials and agent-session variables are excluded. Generated Lake outputs are not source inputs. These fingerprints identify
built inputs; they do not establish proof correctness or make writable caches or local receipts a
security boundary. Verification never weakens the axiom checks.

Heavy phases acquire one host-shared flock slot, released between phases and never held during the
conversation. `TAUCETI_VERIFY_HEAVY_SLOTS` must be a positive integer and defaults to 1. All workers
on a host should use the same setting. The default lock directory uses the real host boot/machine
identity and UID under `/tmp`, independent of worker ID, HOME, checkout or fleet labels. The directory
must be owned by the user and not writable by others. Bubble mounts the host's same directory at
`/opt/verification-queue` and persists receipts back under worker state. Other container deployments
must bind a common host directory via `TAUCETI_VERIFY_HOST_QUEUE`; isolated container filesystems
cannot coordinate flocks. The mounted directory must remain accessible with the same user ownership;
UID-remapped deployments must configure their mounts accordingly. Unsupported ownership fails the helper
with incomplete evidence, rather than aborting review rounds or V0 startup. Separate Unix users need
an explicitly shared queue arrangement. Bubble's preliminary build also uses the queue.

Waiting prints and publishes `waiting-verification-capacity`, keeps the existing candidate/claim
heartbeat running, performs no claim reads, and observes the round deadline. Locks are polled once
per second; status is refreshed at most every fifteen seconds. Each invocation resets its wait record
and holds a separate live-invocation flock, so a cancelled helper cannot grant a later successful
invocation a stale retry refund. It does not alter
inference admission. An interrupted queue wait records unfinished work and a durable counter journal;
the worker refunds PR attempt counters idempotently during cleanup or before the next survey after
a crash. Normal completion closes the attempt journal. The loop consults fresh local journals for
host and Bubble rounds and uses exit 78 for verification-capacity waits, outside both no-work streaks
and API budget/admission accounting.

`TAUCETI_VERIFY_CHECKPOINT_MARGIN` reserves 600 seconds before the round deadline by default (a
nonnegative number of seconds; fixtures explicitly use zero). At that boundary the helper returns
incomplete with “checkpoint now”. Bubble candidate edits are saved as a base-bound binary patch and
an untracked-file archive in `verification-checkpoints/` before checks begin, including when there is
insufficient time to start verification. These are recovery artifacts, never automatic recovery or
proof of success. Automatic recovery remains governed by #310. Claude Bash timeout variables cover
the remaining round; Codex prompts instruct the agent to wait on its returned execution session.
SIGINT, SIGTERM and SIGHUP cause owned cleanup and incomplete receipts.

Tools use #306's `containment.Boundary`. Strong hosts contain children in owned PID namespaces;
legacy hosts accurately report best-effort process-group cleanup. Bubble owns its outer boundary and
uses the legacy inner fallback. Normal exits check each command directly; shell lint drivers use
Bash pipefail. No process-name polling is used. Lock descriptors are never inherited by tools, so
owner death releases capacity. On legacy hosts, a SIGKILLed owner can leave an escaped child running;
strong containment is required for automatic recovery under #310's separate gate.

Set `TAUCETI_VERIFY_HELPER=0` to restore the bundled V0 authoring/repair prompts and scope recipe at
the next round. This changes dispatch only: it does not delete receipts/checkpoints or terminate an
active helper. Host slot count and containment policy are also read at the next phase/boundary.
Local fixture tests do not start paid agents or production rounds. Natural production observations
are still required to assess completed useful work; no throughput improvement is asserted.

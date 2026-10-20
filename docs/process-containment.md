# Process containment

A host round's agent and tool processes each run as the first process of their own rootless
bubblewrap PID namespace: the coding agent, cache preparation (downloads with or without
shared Mathlib, and the `lake env` calls that select a shared snapshot), the Codex
model-access probe, the review engine and the security-audit client. Everything those
processes start stays inside the namespace, including tools that call `setsid` or
double-fork. When the first process exits, the kernel kills everything left in the
namespace before bubblewrap returns. `--die-with-parent` does the same if the worker is
SIGKILLed. One-shot `tauceti work`, `--loop` rounds, cache preparation and both
shared-Mathlib modes all use it. When the shared Mathlib view is active, its overlay
namespace is the boundary, so namespaces are never nested.

The round supervisor stays outside: the round lock, branch claims, activity and usage
accounting, and recovery journals. It confirms each namespace is empty before
continuing, so a checkout is only cleaned up once nothing can still write to it. If the
namespace outlives its first process, the supervisor kills that namespace's own init,
addressed by a pidfd taken at launch, and nothing else. On timeout or interruption, it
kills the namespace before draining its output. It never searches the host for processes.
If emptiness cannot be confirmed, the round fails rather than reusing the checkout. A
marker under the activity directory then pauses automatic recovery until that namespace
is observed gone. `tauceti doctor` lists such markers. One with no reported namespace
(bwrap killed before it reported one) cannot retire by itself: remove it once nothing
from that round is running. Losing a branch claim cancels the agent working on that branch, which
tears its namespace down.

One window remains: bubblewrap arms `--die-with-parent` in the namespace's init a few
milliseconds after creating it. The supervisor waits out that setup before treating the
launch as complete, and kills the init itself if interrupted meanwhile. A supervisor that
is SIGKILLed inside those milliseconds can still orphan the namespace.

Filesystem, network, credentials and provider sandboxes behave as before: the whole
filesystem and `/dev` are bound unchanged. As inside any unprivileged user namespace, setuid programs such as `sudo` do
not gain privileges.

At the end of its cleanup, each round writes `state/<worker>/quiescence.json` for
[work recovery](work-recovery.md). It marks the round empty only when this host contains
every launch and each of the round's boundaries was confirmed empty. Any best-effort,
Bubble-owned or unconfirmed boundary withholds that proof. A round killed before its
cleanup writes nothing, so its dirty checkout stays retained as unknown.

Each boundary is recorded with its round as activity receipts: `containment_started` at
launch, with the label, mode, namespace init pid and PID namespace inode, then
`containment_result` with the exit status and cleanup result.
Command lines are never recorded, because they can carry prompts and credentials.

## Capability and rollback

`tauceti doctor` runs a real namespace and reports the result on its `containment` row.
No root daemon or setuid helper is needed. On Linux, install bubblewrap and allow
unprivileged user namespaces. On Ubuntu that means an AppArmor profile for `bwrap`, or
`kernel.apparmor_restrict_unprivileged_userns=0`.

`TAUCETI_PROCESS_CONTAINMENT` selects the policy:

- `auto` (the default) contains wherever the probe succeeds. Otherwise it falls back to
  the earlier best-effort process-group cleanup and says so in `doctor` and in each
  round's receipts. That cleanup cannot reach a process that left its session.
- `required` refuses to launch a host agent or tool without a namespace.
- `legacy` is the rollback for a provider that misbehaves inside a namespace. It restores
  best-effort process-group cleanup explicitly. It also turns off the shared Mathlib view,
  which is itself a namespace: rounds use private builds, or pause under
  `TAUCETI_SHARED_MATHLIB_BUILDS=required`.

Non-Linux hosts keep running with best-effort cleanup and report it accurately.
Whenever containment is not strong (rollback, unsupported host, or `required` that cannot
be met), or an earlier boundary was never confirmed empty, automatic security-audit
recovery is paused, because a relaunched client could
again outlive its round. Recover manually with `security_audit resume`.

The policy is read when each boundary starts, so a change applies at the next round
boundary. Active rounds keep the containment they started with. Validate on one host
with `tauceti doctor`, then watch naturally selected rounds' receipts.

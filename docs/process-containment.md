# Process containment

A host round's long-running processes each run as the first process of their own rootless
bubblewrap PID namespace: the coding agent, cache preparation downloads (with or without
shared Mathlib), the review engine and the security-audit client. Everything those
processes start stays inside the namespace, including tools that call `setsid` or
double-fork. When the first process exits, the kernel kills everything left in the
namespace before bubblewrap returns. `--die-with-parent` does the same if the worker is
SIGKILLed. One-shot `tauceti work`, `--loop` rounds, cache preparation and both
shared-Mathlib modes all use it. When the shared Mathlib view is active, its overlay
namespace is the boundary, so namespaces are never nested.

The round supervisor stays outside: the round lock, branch claims, activity and usage
accounting, and recovery journals. It confirms each namespace is empty before
continuing, so a checkout is only cleaned up once nothing can still write to it. If the
namespace outlives its first process, the supervisor kills that namespace's own init and
nothing else. It never searches the host for processes. Filesystem, network, credentials
and provider sandboxes behave as before: the whole filesystem and `/dev` are bound
unchanged. As inside any unprivileged user namespace, setuid programs such as `sudo` do
not gain privileges.

Each boundary is recorded with its round as a `containment_result` activity receipt: the
label, mode, namespace init pid, PID namespace inode, exit status and cleanup result.
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
  best-effort process-group cleanup explicitly.

Non-Linux hosts keep running with best-effort cleanup and report it accurately.
Whenever containment is not strong (rollback, unsupported host, or `required` that cannot
be met), automatic security-audit recovery is paused, because a relaunched client could
again outlive its round. Recover manually with `security_audit resume`.

The policy is read when each boundary starts, so a change applies at the next round
boundary. Active rounds keep the containment they started with. Validate on one host
with `tauceti doctor`, then watch naturally selected rounds' receipts.

# Docker deployment

The Compose deployment packages TauCetiWorker, Lean, GitHub CLI, Codex, Claude
Code, and the checksum-pinned Kiro CLI for an unattended Linux host.

## Requirements

- Docker with Compose v2, either as `docker compose` or the standalone `docker-compose`
- At least 8 GB of RAM for Lean builds
- Roughly 25 GB of free disk for the image, toolchain, and Mathlib cache
- GitHub access plus credentials for whichever of Codex, Claude, and Kiro you use

## Setup

Run these commands from the repository root. Follow the prompts from each login
command; the temporary setup containers are removed, while credentials remain in
named volumes. If `docker compose` is unavailable, replace it with `docker-compose`.

```bash
docker compose build
docker compose run --rm auth gh auth login --git-protocol https
docker compose run --rm auth codex login --device-auth
docker compose run --rm auth claude auth login
docker compose run --rm auth kiro-cli login
```

Start the worker and follow its logs:

```bash
docker compose up -d
docker compose logs -f tauceti claude-refresh codex-refresh
```

Starting the deployment also starts the credential refreshers. They copy access-only
credentials into the volumes mounted by the worker, so they must run before checking
the worker environment. Once they have started, an optional check is:

```bash
docker compose run --rm tauceti ./tauceti doctor
```

Codex, Claude, and Kiro credentials should report `[ok]` when configured. Missing `bubble`, `incus`, and `pi`
are expected for the standard host-mode deployment; they are only needed for Bubble
sandboxing or the DeepSeek and MiniMax agents. `shared Mathlib` is also `[MISSING]` in
this default deployment, which therefore runs review rounds only: see
[Rounds that need Mathlib](#rounds-that-need-mathlib).

## Rounds that need Mathlib

Review rounds run in the default deployment. Every other round (a new PR, a fix, a
rebase) needs Mathlib, and the worker only provides Mathlib through a
[shared build](shared-mathlib.md): a bubblewrap mount namespace, created inside a user
namespace, that overlays the round's private files on one read-only copy. Docker's
default seccomp and AppArmor profiles forbid creating those namespaces, so in the
default deployment such a round stops before its agent starts, the log says

```text
shared Mathlib preparation unavailable: rootless OverlayFS probe failed: bwrap: Creating new namespace failed: Operation not permitted
```

and the worker backs off. `tauceti doctor` reports the same reason.

`compose.shared-mathlib.yaml` is an opt-in override that allows those rounds. On a host
with AppArmor (Debian, Ubuntu), load its profile first; this lasts until the next
reboot unless you also copy the file to `/etc/apparmor.d/`:

```bash
sudo apparmor_parser --replace docker/apparmor-shared-mathlib
```

On a host without AppArmor, delete the `apparmor=` line from
`compose.shared-mathlib.yaml` instead. Then name both files in every Compose command:

```bash
docker compose -f compose.yaml -f compose.shared-mathlib.yaml up -d
docker compose -f compose.yaml -f compose.shared-mathlib.yaml run --rm tauceti ./tauceti doctor
```

or put `COMPOSE_FILE=compose.yaml:compose.shared-mathlib.yaml` in `.env` and keep using
the plain commands of this page. `doctor` must now report

```text
[ok     ] shared Mathlib rootless OverlayFS works; immutable lower with private copy-on-write builds
```

Run `doctor` with `run`, as above, or as
`docker compose exec tauceti tauceti-entrypoint ./tauceti doctor`: a bare
`docker compose exec tauceti ./tauceti doctor` starts outside the worker's user
namespace and reports `[MISSING]` even though the worker's rounds run.

### What the opt-in changes

It adds no capability, no device and no host namespace, and it is not `privileged`. It
changes four things for the `tauceti` service only:

| Setting | What it does | Why it is needed |
|---|---|---|
| `seccomp=docker/seccomp-shared-mathlib.json` | Docker's default profile (release 28.0.4) plus one rule that allows `clone` with namespace flags, `unshare`, `mount`, `umount2`, `pivot_root` and `mount_setattr` to processes without `CAP_SYS_ADMIN` | `unshare` creates the worker's user namespace; bubblewrap then uses `clone` for its mount namespace, `mount` and `mount_setattr` for the binds and overlays, and `pivot_root` and `umount2` to enter the new root. Removing any one of the six makes the probe fail. |
| `apparmor=tauceti-shared-mathlib` | Docker's default profile with `deny mount` replaced by `mount, pivot_root` | The default profile denies every mount, in any namespace. A named profile is also what lets the container create a user namespace on Ubuntu 23.10 and later, which refuses that to unconfined programs (so `apparmor=unconfined` does not work there). |
| `TAUCETI_USER_NAMESPACE=1` | The entrypoint re-runs itself, and so the worker, as root of a new user namespace in which every user and group id maps to itself | The image runs as root, and bubblewrap run by root creates no user namespace: it would need `CAP_SYS_ADMIN` over the host. Inside the namespace the worker has every capability over the namespace and none over the host. File ownership is unchanged. |
| `shared-mathlib` volume at `/root/.cache/tauceti` | Keeps the shared Mathlib pool on a volume | The worker probes OverlayFS in the pool, and an overlay's writable layer cannot live on the container's own overlay filesystem. The volume also keeps Mathlib (about 8 GB) across image updates. |

The kernel honours those six system calls, for a process that has no capability over
the host, only inside a user namespace the process owns, where they act on that
namespace's own mounts. The container's view of `/proc` and `/sys` keeps Docker's
masking: with the opt-in, mounting a fresh `/proc` is still refused and `/proc/sys`
is still read-only.

### What that costs in isolation

Docker forbids user namespaces in containers by default for a reason. Root of a user
namespace can reach kernel code (mounting filesystems, above all) that an ordinary
process cannot, and bugs in that code have been used to escape containers. With the
opt-in, the agents, which run arbitrary commands in this container, can reach that
code. The container is then about as well confined as an unprivileged user on an
ordinary Linux host, which is what a worker installed without Docker already is; it is
less confined than the default container. Loosening AppArmor's `deny mount` matters
for the same reason and no other.

Settings that also make the probe pass and that this deployment deliberately does not
use: `cap_add: SYS_ADMIN` with `apparmor=unconfined` (the container's root can then
mount a fresh `/proc` and more, which is close to root on the host), and `privileged`.

If that cost is too high for your host, keep the default deployment for reviews and
run the authoring worker in a virtual machine, installed as the
[README](../README.md) describes: there the user-namespace surface is the virtual
machine's kernel, not the host's.

The opt-in has been exercised on GitHub's `ubuntu-24.04` runners (Docker 28.0.4,
AppArmor 4.0, kernel 7.0), where CI runs the capability probe and the worker's overlay
view tests in the built image on every change. It has not been tried with rootless
Docker, `userns-remap`, Docker Desktop, Podman or an SELinux host. The seccomp profile
is a copy of one Docker release's default; with a much newer Docker, compare it with
that release's default before relying on it.

## Worker options

Pass options to the long-running worker when starting it:

```bash
TAUCETI_WORKER_ARGS="--only roadmap --roadmap-only Topology" docker compose up -d
```

To keep those options for future `docker compose up` commands, put the setting in
`.env` at the repository root instead:

```dotenv
TAUCETI_WORKER_ARGS=--only roadmap --roadmap-only Topology
```

Then apply it normally:

```bash
docker compose up -d
```

The options are appended to `tauceti work --loop`. Edit or remove the line and run
`docker compose up -d` again to change or clear them; credentials and worker data are
retained.

Several containers on one machine (or across machines) need no coordination
setup: each resolves the same claim namespace and they de-duplicate their work
automatically, so they will not all pick the same job. See
[the claim namespace](reference.md#the-claim-namespace) for how that is resolved
and when to override it with `CLAIM_REPO`. If you are running containers under
two different `gh` accounts and want them treated as one fleet, give them the
same `CLAIM_REPO=<a repo both accounts can push to>` in `.env`.

For headless Kiro auth, set `KIRO_API_KEY` in `.env` instead of running the Kiro
login command. Compose also forwards `OPENROUTER_API_KEY` and
`OPENROUTER_MANAGEMENT_KEY` from `.env` for credit telemetry (and for an
operator-installed `pi` runner). You can inspect either balance without sending
a model prompt:

```bash
docker compose run --rm tauceti ./tauceti usage --json
```

## Operations

Stop the deployment while retaining all data:

```bash
docker compose down
```

Update the checkout and replace the image while retaining volumes:

```bash
git pull
docker compose up -d --build
```

To erase credentials, checkouts, caches, logs, and worker state:

```bash
docker compose down -v
```

This is destructive and requires fresh logins and dependency downloads on the
next start.

## Persistent storage

| Volume | Contents |
|---|---|
| `claude`, `codex` | Provider credentials, writable only by setup and the corresponding refresher |
| `kiro` | Kiro browser-login database, writable by setup and snapshotted into the isolated worker home |
| `claude-worker`, `codex-worker` | Access-token mirrors, writable by refreshers and mounted read-only by the worker |
| `gh` | GitHub CLI credentials |
| `uv-cache` | Downloaded Python tools and packages |
| `checkouts` | Worker repositories and incremental Lean build artifacts |
| `state` | Scheduler state and isolated worker home |
| `logs` | Per-round logs |
| `shared-mathlib` | Only with `compose.shared-mathlib.yaml`: the shared Mathlib pool and build caches |

Claude and Codex use rotating, single-consumer refresh tokens. One refresher owns
each provider credential and publishes a refresh-token-free mirror; the worker never
mounts the source provider credentials.

The refreshers check once a minute, renew within 90 minutes of expiry, avoid rotating
more than once per 10 minutes, and back off to 15 minutes after errors. Advanced
deployments can override these service environment variables:

- `TAUCETI_REFRESH_POLL_SECONDS`
- `TAUCETI_REFRESH_SKEW_SECONDS`
- `TAUCETI_REFRESH_MIN_INTERVAL_SECONDS`
- `TAUCETI_REFRESH_MAX_BACKOFF_SECONDS`

## Security

This deployment runs agents in host mode inside the worker container, not in a
Bubble round. Agents can access their provider access tokens and the GitHub credential
and have unrestricted network access. Use it only on a trusted, dedicated Docker host.
The opt-in for rounds that need Mathlib loosens the container's confinement further;
[What that costs in isolation](#what-that-costs-in-isolation) says how.

The deployment was adapted from
[eohjelle/TauCetiWorker-docker](https://github.com/eohjelle/TauCetiWorker-docker).

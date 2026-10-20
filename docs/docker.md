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
sandboxing or the DeepSeek and MiniMax agents.

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

The deployment was adapted from
[eohjelle/TauCetiWorker-docker](https://github.com/eohjelle/TauCetiWorker-docker).

Host authoring checkout generations and their private build caches now live under
the worker `state` volume. Size that volume for retained source and build outputs;
retained generations survive rollback. See [work recovery](work-recovery.md).

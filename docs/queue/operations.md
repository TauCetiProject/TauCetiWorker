# AI merge queue operations

Runbook for the queue service specified in
[`docs/redesign/specs/ai-merge-queue.md`](../redesign/specs/ai-merge-queue.md). The GitHub App and
scratch repository entries come from increment 0; the host, deployment, settings, cutover and
monitoring entries are the deployment stream of increment 1.

## GitHub App

The service acts through its own GitHub App, `TauCeti Queue`, owned by `TauCetiProject`. The
increment 0 shadow repair task and the status endpoint run on its API budget, never on a personal
account's rate limit.

The App exists: slug `tauceti-queue`, App ID `5607007`, created 2026-11-04, with the permissions and
events below confirmed from `gh api /apps/tauceti-queue`. It is installed on `TauCetiProject/TauCeti` only (installation `178451644`, verified on 2026-11-04 by
authenticating with the stored key and listing the installation's repositories). Its webhook is
`https://queue.taucetiproject.org/webhook/github`, active since 2026-11-04 22:45 UTC (Public endpoint,
step 7).

Registration is prefilled by:

```sh
uv run python scripts/tauceti_queue_github_app.py url
```

Open the printed URL as an organization owner, set a webhook secret in the form (GitHub does not
accept one through the URL), and create the App. The URL requests exactly these repository
permissions: contents write, pull requests write, issues write, checks read, statuses write,
actions write, workflows write, metadata read; and subscribes to these events: pull request,
pull request review, issue comment, status, check run, check suite, push, workflow run. Pass
`--webhook-url https://<host>/webhook/github` once the service host exists. Without it GitHub creates
the App with no webhook and discards the event subscription, so both are added on the App's settings
page later (Public endpoint, step 7); the API can edit an App webhook but not create one.

After creation, generate a private key on the App's settings page and record it together with the
App ID and slug:

```sh
uv run python scripts/tauceti_queue_github_app.py save --app-id <id> --slug <slug> < ~/Downloads/tauceti-queue.*.private-key.pem
```

The credentials live in `~/.config/tauceti-queue/` on the service host, mode 0600, next to the
existing `~/.config/tauceti-bors/` layout:

| File | Content |
| --- | --- |
| `github-app.json` | App ID, slug, owner, the requested permissions and events |
| `github-private-key.pem` | the App's private key |
| `github-webhook-secret` | the webhook secret entered in the form |

Install the App on `TauCetiProject/TauCeti` and on the scratch repository
`TauCetiProject/TauCetiQueueScratch` only.

## Scratch repository

`TauCetiProject/TauCetiQueueScratch` carries the TauCeti workflow files and a small Lean library so
the cutover rehearsal (work plan stream I) can run every acceptance scenario the repository can
express against synthetic PRs. The service treats it as an opted-in repository like any other.

## Host and staging capacity

The service runs on one fleet host as a systemd user service with a single SQLite database file (WAL
mode) and a public HTTPS endpoint for webhooks through a Cloudflare tunnel in the existing `tauceti`
Cloudflare account. The sections below describe the host, the layout, installation and live updates,
backups, verification, the runtime settings, the cutover and what to watch. Staging builds keep running
on the fleet hosts as today (operator decision, 2026-11-04; measurements in
[staging-runners.md](staging-runners.md)); the service records which runner and host ran each candidate
build and how long each phase took, and makes placement decisions from those measurements later.

## Host requirements

- A fleet host with an ordinary Linux account (the same account as the fleet's workers is fine), systemd
  user services and lingering enabled (`loginctl enable-linger USER`, an administrator step) so the
  service survives logout and reboots. The operator names the host; the first candidate is the host
  that already holds the App credentials (`chungus2`).
- `git`, `uv`, `python3` (3.11 or newer; `uv` provides the interpreter) and `cloudflared` on the
  account's PATH. The unit's PATH covers `~/.local/bin`, `/run/current-system/sw/bin`,
  `/usr/local/bin`, `/usr/bin` and `/bin`.
- Outbound HTTPS to GitHub and Cloudflare. No inbound port: the tunnel connects outward, and the service
  listens on `127.0.0.1:8780` only.
- Disk for the content clone of `TauCetiProject/TauCeti` (a few GiB), the database (small) and its
  daily backups (14 copies by default).
- The App credentials from the GitHub App section in `~/.config/tauceti-queue/`, mode 0600.

## Layout on the host

| Path | Content |
| --- | --- |
| `~/TauCetiQueue` | checkout of `TauCetiProject/TauCetiWorker` at the installed revision; the unit runs `uv run tauceti-queue serve` from here |
| `~/.config/tauceti-queue/config.toml` | the configuration (below); created from `services/queue/config.toml.example` on first install |
| `~/.config/tauceti-queue/environment` | optional `TAUCETI_QUEUE_*` overrides read by the unit (`EnvironmentFile`); never secrets |
| `~/.config/tauceti-queue/github-app.json`, `github-private-key.pem`, `github-webhook-secret` | the App credentials (GitHub App section) |
| `~/.config/systemd/user/tauceti-queue.service` | the service unit, copied from `services/queue/` by the installer |
| `~/.config/systemd/user/tauceti-queue-backup.{service,timer}` | the daily backup |
| `~/.config/systemd/user/tauceti-queue-repair.service`, `~/.config/tauceti-queue/repair-environment` | the repair worker unit and its environment file (Repair worker below), on the host that runs repair tasks |
| `~/.local/state/tauceti-queue/queue.sqlite3` (+ `-wal`, `-shm`) | the database: approved inputs, batches, claims, CI attempts, pending side effects, settings |
| `~/.local/state/tauceti-queue/backups/` | dated online backups |
| `~/.local/state/tauceti-queue/logs/` | the service's own files (webhook delivery journal); process logs go to journald |
| `~/.local/share/tauceti-queue/content` | the content clone used for chain construction and replay |
| `~/.cloudflared/config.yml`, `~/.cloudflared/<tunnel id>.json` | the tunnel configuration and credentials (Public endpoint) |

## Configuration

`tauceti_queue/config.py` reads defaults, then `~/.config/tauceti-queue/config.toml` (or the file named
by `TAUCETI_QUEUE_CONFIG` / `--config`), then `TAUCETI_QUEUE_<KEY>` environment variables, later layers
winning. Unknown keys are errors. `uv run tauceti-queue check-config` prints every effective value with
its source (`default`, `file`, `env:VARIABLE`, `hostname`) and the problems that would stop `serve`;
`--json` and `--print KEY` serve scripts.

| Key | Default | Meaning |
| --- | --- | --- |
| `repository` | `TauCetiProject/TauCeti` | the opted-in repository |
| `canonical_branch` | empty | the branch the service promotes to; empty means discovered from the repository's default branch at start (`git ls-remote --symref`), never assumed |
| `host` | this machine's host name | the runner placement of the service, reported in status |
| `publication_enabled` | `false` | the cutover flag; `false` is shadow mode (observe, build batches, publish nothing) |
| `content_clone` | `~/.local/share/tauceti-queue/content` | the content clone |
| `database` | `~/.local/state/tauceti-queue/queue.sqlite3` | the SQLite file |
| `credentials_dir` | `~/.config/tauceti-queue` | where the three App credential files live |
| `log_dir` | `~/.local/state/tauceti-queue/logs` | the service's own log files |
| `listen_host`, `listen_port` | `127.0.0.1`, `8780` | the local listen address the tunnel forwards to |
| `public_url` | `https://queue.taucetiproject.org` | the public base; the webhook address is `<public_url>/webhook/github` |
| `reconcile_interval_seconds` | `30` | the loop's cadence between event-driven wakeups |
| `review_app_id` | `3947238` | the GitHub App whose `merge eligibility` checks and exact-head comments are approvals (TauCetiReview); the rehearsal points it at the queue's own App on the scratch repository |
| `[settings] insertion_age_window` | `16` | initial runtime control (plan, "Runtime controls") |
| `[settings] repair_concurrency` | `1` | initial runtime control |
| `[settings] stall_interval_seconds` | `900` | initial runtime control |
| `[settings] approval_poll_interval_seconds` | `900` | how often the approval poll lists the open PRs' `merge eligibility` checks behind the webhooks (Approval polling below) |

The `[settings]` values seed the store on first start. After that the persisted settings are the
effective ones and are changed through the admin endpoint (Runtime settings below), not by editing the
file; status reports the effective values.

## Install and update

Both are the same command, run on the host as the service account, and both are live: the fleet keeps
admitting workers and CI, nothing is drained, and the only interruption is the service restart, during
which webhook deliveries are queued by GitHub and redelivered or picked up by the fallback poll.

```sh
# first time: get the installer, then let it manage the checkout
git clone https://github.com/TauCetiProject/TauCetiWorker.git ~/TauCetiQueue
~/TauCetiQueue/services/queue/install.sh                     # origin/main
~/TauCetiQueue/services/queue/install.sh --revision <sha>    # a pinned revision
```

`services/queue/install.sh` is idempotent. It fetches, fast-forwards the checkout to the requested
revision (refusing local modifications or a non-fast-forward target rather than forcing), runs `uv
sync`, bakes the revision into `tauceti_queue/_revision.py`, writes `config.toml` and `environment` from
the examples when absent, runs `check-config`, installs the units, `systemctl --user daemon-reload`,
enables and restarts `tauceti-queue.service` and the backup timer, and then verifies the deployment by
reading `/status` until the service answers and comparing the reported `service.revision` with the
checkout. Exit 0 means the running service is the installed revision; exit 2 means it answered with a
different revision or not at all, and the next step is `journalctl --user -u tauceti-queue -n 50`.

`--no-restart` installs without restarting (the running service keeps its revision; verification then
reports the mismatch deliberately). The unit has `Restart=always`, `TimeoutStopSec=120` and
`KillSignal=SIGTERM`: on stop the process finishes its current reconciliation step, stops the HTTP
server and closes the store before exiting, so a restart never interrupts a store write.

On a host without lingering (Current deployment, "Hosts without lingering") the units are disabled and
a deploy is `install.sh --revision <sha> --no-restart` followed by `pkill -f 'tauceti-queue serve'`;
the supervisor loop restarts the service on the new revision.

Useful commands:

```sh
systemctl --user status tauceti-queue
journalctl --user -u tauceti-queue -f
uv run tauceti-queue status                     # one-screen summary from the local port
uv run tauceti-queue status --json | jq .service
uv run tauceti-queue status --url https://queue.taucetiproject.org   # through the tunnel
```

## Current deployment

Running on `chungus` since 2026-11-05 01:13 UTC, revision `d84649118232`, as the account `kim`. Both
`queue.taucetiproject.org` and `bors.taucetiproject.org` report `service.host` `chungus` since
01:13:33 UTC. The operator has no root on chungus and lingering is not enabled for the account
(`loginctl enable-linger` is denied), so the service, the tunnel and the repair worker run as detached
supervised processes started by hand (Hosts without lingering below); the units are installed but
disabled. **chungus is a temporary host too.** A fresh dedicated machine is expected about eight hours
after the move; the service moves there by the procedure below, restoring the systemd units and the
backup timer.

History: the service was installed on `tauchonki` on 2026-11-04 (revision `045c7e58`, updated live to
`cc5270bc`), got its tunnel, public hostname and App webhook there at 22:37 to 22:45 UTC and became
the production merge backend from there at 22:58 UTC. tauchonki also runs the fleet's staging builds
and many Claude sessions; on 2026-11-05 its load sat between 150 and 490, one controller pass took
ten minutes and the public endpoints timed out, so the operator moved the service. At 01:10 UTC the
service and the backup timer were stopped on tauchonki (last backup `queue-20261105T011003Z.sqlite3`),
`~/TauCetiQueue-data` (database with WAL, logs, content clone) was copied to chungus by tar over ssh
together with the App credentials, `config.toml` (`host` set to `chungus`) and the tunnel files
(`~/.cloudflared/cert.pem`, `config.yml`, `be28d750-6092-4526-8aed-76a0d0bf7c66.json`);
`install.sh --revision d84649118232 --no-restart` installed the checkout, and at 01:13 the service and
the tunnel were started on chungus. On tauchonki the units `tauceti-queue`, `tauceti-queue-backup.timer`
and `cloudflared-tauceti-queue` are disabled and `~/TauCetiQueue-data` is kept for a day.

Paths on chungus, where they differ from the layout table:

| Path | Content |
| --- | --- |
| `~/TauCetiQueue` | the checkout, shared by the service and the repair worker; `tauceti_queue/_revision.py` baked by the installer |
| `~/.config/tauceti-queue/config.toml` | copied from tauchonki with `host = "chungus"`, `review_app_id = 3947238`, the data paths below |
| `~/TauCetiQueue-data/clone` | the content clone of `TauCetiProject/TauCeti` (`content_clone`) |
| `~/TauCetiQueue-data/queue.sqlite3` (+ `-wal`, `-shm`) | the database (`database`) |
| `~/TauCetiQueue-data/logs` | the service's own files (`log_dir`) |
| `~/TauCetiQueue-data/run-service.sh`, `service.log` | the service supervisor loop (`uv run tauceti-queue serve`) and the process log, which replaces journald |
| `~/TauCetiQueue-data/run-tunnel.sh`, `tunnel.log` | the tunnel supervisor loop (`cloudflared tunnel ... run`) and its log |
| `~/TauCetiQueue-data/repair-clone`, `~/TauCetiQueue-data/repair`, `repair-worker.log` | the repair worker's content clone, task root and log |
| `~/.local/state/tauceti-queue/backups/` | backups, taken by hand (no timer) |
| `127.0.0.1:8780` | the listen address |
| `~/.cloudflared/cert.pem`, `config.yml`, `<tunnel id>.json` | the Cloudflare origin certificate, tunnel configuration and tunnel credentials, copied from tauchonki |
| `~/.config/systemd/user/tauceti-queue.service`, `tauceti-queue-backup.{service,timer}`, `tauceti-queue-repair.service` | installed by the installers, disabled |

### Hosts without lingering

Without lingering (`loginctl enable-linger` needs an administrator) systemd user units die when the
account's last login session ends, so the three processes run detached under `nohup setsid`, each from
a login shell on the host. The installers still run with `--no-restart` so the checkout, the
configuration and the units are in place; the units stay disabled.

```sh
cd ~/TauCetiQueue-data
nohup setsid ./run-service.sh >/dev/null 2>&1 &      # loops `uv run tauceti-queue serve`; log service.log
nohup setsid ./run-tunnel.sh >/dev/null 2>&1 &       # loops `cloudflared tunnel ... run`; log tunnel.log
cd ~/TauCetiQueue && nohup setsid uv run python -m tauceti_queue.repair_task \
  --service https://queue.taucetiproject.org --worker repair@chungus \
  --clone ~/TauCetiQueue-data/repair-clone --root ~/TauCetiQueue-data/repair \
  >~/TauCetiQueue-data/repair-worker.log 2>&1 &
pgrep -af 'run-service.sh|run-tunnel.sh|tauceti-queue serve|cloudflared tunnel|repair_task'
```

Consequences:

- **File-descriptor limit.** Detached processes do not get the units' `LimitNOFILE=65536`; they
  inherit the login's limit of 1,024, which a large repair exhausts. The supervisor scripts run
  `ulimit -n 65536` first; start the repair worker from a shell that has done the same.
- **No restart after a reboot.** After a reboot of chungus, or whenever `pgrep` shows a process
  missing, the operator or an agent starts the three again by hand as above. The repair worker has no
  loop around it, so a crash leaves it down until restarted.
- **No daily backup.** The timer is disabled; run `~/TauCetiQueue/services/queue/backup.sh` by hand
  before any risky change, or from a cron entry (`17 3 * * * ~/TauCetiQueue/services/queue/backup.sh`).
- **Deploy** is `~/TauCetiQueue/services/queue/install.sh --revision <sha> --no-restart` followed by
  `pkill -f 'tauceti-queue serve'`; the supervisor loop restarts the service on the new revision
  (the SIGTERM shutdown is the same as the unit's). Verify as in "Verification after a deploy",
  reading `service.log` instead of `journalctl`. The repair worker shares the checkout, so after a
  deploy that changes it, `pkill -f tauceti_queue.repair_task` (a running task is checkpointed and
  resumed) and start it again.
- **Stop** the service or the tunnel for good by killing the loop script before its child
  (`pkill -f run-service.sh; pkill -f 'tauceti-queue serve'`); killing only the child restarts it.

### Moving to the dedicated host

1. On the new host, meet the Host requirements (account, lingering, `git`, `uv`, outbound HTTPS).
   Create `~/.config/tauceti-queue/` with mode 0700 and copy the three credential files and
   `config.toml` from chungus with `scp -p` (mode 0600; compare checksums, never print them). Set
   `host` in the copied `config.toml` to the new host's name.
2. On chungus, stop the service so the store is closed and the WAL checkpointed: kill the loop script
   and then the service (`pkill -f run-service.sh; pkill -f 'tauceti-queue serve'`; on a host with
   enabled units, `systemctl --user stop tauceti-queue tauceti-queue-backup.timer`). Stop the repair
   worker the same way once `worker.json` reports `idle`. Take a last backup
   (`~/TauCetiQueue/services/queue/backup.sh`).
3. Copy the data directory: `rsync -a ~/TauCetiQueue-data/ <newhost>:~/TauCetiQueue-data/` (the
   database with its `-wal` and `-shm` files, the content clone and the logs). Copying the clone
   saves the fresh clone the installer would otherwise make.
4. On the new host, install at the revision chungus was running (or `main`, if a newer revision is
   wanted; the store schema is migrated by the service):
   `git clone https://github.com/TauCetiProject/TauCetiWorker.git ~/TauCetiQueue && ~/TauCetiQueue/services/queue/install.sh --revision <sha>`.
   The installer reports `content clone present`, runs `check-config`, enables the unit and the backup
   timer and verifies the revision. Install the repair worker with `install-repair.sh`.
5. Verify as in "Verification after a deploy": `service.host` is the new host, `canonical_sha` equals
   chungus's last value, `settings` are the persisted ones (the store wins over the file), and
   `counters` carried over. Then stop the tunnel loop on chungus, copy `~/.cloudflared/` to the new
   host and enable the `cloudflared-tauceti-queue` unit there (or run the Public endpoint steps).
6. On chungus, remove the disabled unit files from `~/.config/systemd/user/` and `systemctl --user
   daemon-reload`. Keep `~/TauCetiQueue-data` until the new host has run for a day, then delete it
   together with `~/TauCetiQueue` and `~/.config/tauceti-queue`. Update this section and `status.md`.

## Repair worker

When a head's CI fails with attributable diagnostics the service creates a repair task (or a
rebase-conflict task when a canonical advance conflicts); nothing happens to it until a worker claims
it. `tauceti_queue/repair_task.py` ([repair-task.md](repair-task.md)) is that worker, and
`services/queue/tauceti-queue-repair.service` runs it continuously as a systemd user unit: resume
checkpointed tasks, claim, prepare warm artifacts, run the repair agent, push the chain to the task's
submission ref, submit it, report the provider outcome to `/tasks/inference` (the inference hold),
sleep the poll interval when nothing is claimable. A task's failure, a service outage or a refused
authorization is logged and retried on the next pass; the loop never exits for them. The worker runs
on the service host today; it only needs the service URL, so it can run on any host meeting the
requirements below (the service's `repair_concurrency` setting bounds how many hold a task at once).

### Host requirements

- The account's systemd user services with lingering enabled, `git`, `uv` and `gh` on the unit's PATH
  (`~/.local/bin`, `~/.elan/bin`, `~/.nix-profile/bin`, `/run/current-system/sw/bin`, `/usr/local/bin`,
  `/usr/bin`, `/bin`), and outbound HTTPS to GitHub, the Lake artifact cache service and the inference
  provider.
- **Disk.** The task root holds a private Lake artifact pool (every build's outputs are stored there,
  about 13 GB per build of a large candidate; the experiments needed tens of GB per base and were
  stopped by a full disk), one worktree per running task (a full checkout plus its `.lake/build`) and
  the extracted Mathlib (7.7 GB per task unless `TAUCETI_QUEUE_SEED_PACKAGES` shares one). Budget
  50 GB free and prune the pool between bases with
  `uv run python -m tauceti_queue.repair_experiment prune --root <task root>`; the worker's `--check`
  fails below 20 GiB free.
- **Toolchain.** `elan` with the toolchain the content repository pins (`lean-toolchain`, installed on
  first use by the `lake` shim at `~/.elan/bin/lake`), and the fleet's Lake artifact cache reachable
  (`lake cache get --service tauceti-public`). The worker never builds Mathlib: it restores the base's
  outputs from the cache and builds the batch's own modules.
- **Build sandbox.** `bwrap` (bubblewrap) on PATH with unprivileged user namespaces: every `lake
  build` the worker runs is confined the way CI's required build is ([repair-task.md](repair-task.md),
  "Sandboxed builds"), and `--check` runs `true` inside the sandbox. On NixOS it is
  `/run/current-system/sw/bin/bwrap`. A host without it may set `TAUCETI_QUEUE_SANDBOX=0` in the
  environment file, which runs builds unsandboxed and says so in the log on every build.
- **Credentials**, by name; never print their values.
  - The GitHub token of the worker's account, which must be a repository admin on
    `TauCetiProject/TauCeti`: the service applies `security_audit.require_admin` to the login the
    bearer token resolves to at claim and at submit, and the worker applies the same predicate before
    claiming. Source order: `TAUCETI_QUEUE_TOKEN_FILE` (a mode 0600 file), `TAUCETI_QUEUE_TOKEN`,
    `GH_TOKEN`, `GITHUB_TOKEN`, else `gh auth token` from the account's gh login. The same account's
    git credentials push the submission ref (`gh auth setup-git`, or the token in the helper).
  - One credential per provider in the chain `TAUCETI_QUEUE_PROVIDERS` (default `claude,codex,api`;
    [repair-task.md](repair-task.md), "Providers"). `claude`: Claude Code's credentials in
    `$CLAUDE_CONFIG_DIR/.credentials.json` (default `~/.claude`), copied into the task's isolated config
    dir with `ANTHROPIC_API_KEY` unset so the subscription is billed, as in the experiments; `claude` on
    PATH. `codex`: the Codex credential in `$CODEX_HOME/auth.json` (default `~/.codex`, written by
    `codex login`), copied into the task's isolated `CODEX_HOME` as the fleet seeds its isolated
    workers'; `codex` on PATH. `api`: `ANTHROPIC_API_KEY` in the environment file itself (mode 0600),
    which the worker takes out of its own environment at start and gives only to that provider's agent.
    The `claude` binary first on PATH must be recent enough for the configured model; on chungus the
    managed install (2.1.318 on 2026-11-05) must come before any older copy on PATH.
    Leave a provider out of the chain when the host has no credential for it; `--check` reports each
    one named in the chain.
- **Worker cache** (optional): `TAUCETI_REPAIR_CACHE_URL`, `TAUCETI_REPAIR_CACHE_UPLOAD_URL` and
  `TAUCETI_REPAIR_CACHE_KEY_FILE` ([repair-task.md](repair-task.md), "Repair cache publication").
  The bucket and `repair-cache.taucetiproject.org` exist; the R2 write token does not yet, so
  repairs read the namespace and publish nothing.
- **Parallel agents.** `TAUCETI_QUEUE_PARALLEL_AGENTS` (default 3) bounds the concurrent agents within
  one task ([repair-task.md](repair-task.md), "Parallel agents within one task").
- The content clone named by `TAUCETI_QUEUE_CLONE` (the installer creates it).

The chain is tried in order for each task. A subscription provider is eligible only while the fleet
pacer (`tauceti_worker.quota`, the same usage endpoints and `time%:budget%` curve the worker's `--pace`
flag applies) reports it under pace; `TAUCETI_QUEUE_RATE` sets that curve for the repair worker (unset:
the pacer's default, `60:40`). A subscription over its pace or unmeasurable (no credential, a usage
endpoint failure) is skipped with the reason in the journal; a provider that fails to start or answers a
rate-limit or authentication error before doing any work hands the task to the next one at once. Each
task's outcome and `worker.json` (`providers` counts, `last_outcome.provider_attempts`) record every
provider tried or skipped with its reason and seconds. `--providers` and `--rate` override the two
settings on the command line.

### Install and update

```sh
git clone https://github.com/TauCetiProject/TauCetiWorker.git ~/TauCetiQueue   # if not already present
~/TauCetiQueue/services/queue/install-repair.sh                   # origin/main
~/TauCetiQueue/services/queue/install-repair.sh --revision <sha>  # a pinned revision
```

`install-repair.sh` is idempotent and live. It fast-forwards the checkout, runs `uv sync`, writes
`~/.config/tauceti-queue/repair-environment` from `services/queue/repair-environment.example` when
absent (then review it: service URL, worker name, clone, task root, credential locations), clones the
content repository when absent, runs the worker's host checks, installs the unit, enables and
restarts it, and verifies that the unit is active and that the restarted process (the unit's
`MainPID`) reports itself serving in `<task root>/worker.json`. Exit 0 means the worker is serving;
1 a setup or host-check failure; 2 an inactive or silent unit, and the next step is
`journalctl --user -u tauceti-queue-repair -n 50`. `--no-restart` installs without interrupting a
running task. The queue service, its backup timer and the fleet units are not touched.

A restart (or `systemctl --user stop`) sends SIGTERM: a running agent is stopped, its claim and
checkpoint are kept (`tasks/<id>/task.json` in state `interrupted`, the worktree in place), and the
restarted worker renews the claim and resumes the same model session. A task that has already
validated its chain finishes the push and submission first. `TimeoutStopSec=300`; a worker killed
beyond that leaves the claim to expire (`claim_ttl_seconds`, 900) and the task is re-exposed.

### Verify, logs, stop

```sh
cd ~/TauCetiQueue && uv run python -m tauceti_queue.repair_task --check   # tools, token, admin, service, clone, credentials, disk
systemctl --user status tauceti-queue-repair
journalctl --user -u tauceti-queue-repair -f
jq . "$(. ~/.config/tauceti-queue/repair-environment; echo "$TAUCETI_QUEUE_TASK_ROOT")/worker.json"
```

`--check` prints one line per host requirement and exits 1 when any fails, naming the token's source
and the login, never the token, and whether the api key is set, never the key. `worker.json` is the
worker's heartbeat: `pid`, `state` (`starting`, `claiming`, `running`, `idle`, `stopping`, `stopped`),
`tasks`, `outcomes` by status, `providers` (per provider: `ran`, `skipped`, `failed` counts), `errors`
and `last_error`, `last_outcome` (with the task's `provider` and `provider_attempts`), `started_at` and
`updated_at`. The service side of the same facts is
`status.head.claim` (the worker name and expiry while a task is claimed) and the task's outcome in
the status document. Each task's record is `<task root>/tasks/<id>/` (`task.json`, `outcome.json`,
`logs/` with the agent transcript, prompt, push and build logs).

To stop the worker: `systemctl --user stop tauceti-queue-repair` (a running task is interrupted and
resumed at the next start, as above); to stop it from coming back, `systemctl --user disable --now
tauceti-queue-repair`. To stop without interrupting a task (finish the current one, claim no more),
stop it right after `worker.json` reports `idle`. On a host without lingering (Current deployment) the
worker is a detached process: its log is `~/TauCetiQueue-data/repair-worker.log`, it is stopped with
`pkill -f tauceti_queue.repair_task` (the same SIGTERM path) and started again by hand.

## Public endpoint

`queue.taucetiproject.org` is served by a Cloudflare tunnel from the host to the local port. No
existing service in this organization uses a tunnel (bors and the GitHub state service are Cloudflare
Workers), so the steps are operator instructions in the `tauceti` Cloudflare account
(`ec2169bdf033f56b009956d4b64ba8ef`, zone `taucetiproject.org`):

1. Install `cloudflared` on the host (NixOS: `nix profile install nixpkgs#cloudflared`, or the package
   manager's `cloudflared`).
2. `cloudflared tunnel login` (opens a browser URL; choose the `taucetiproject.org` zone), then
   `cloudflared tunnel create tauceti-queue`. Note the tunnel UUID; the credentials file is written to
   `~/.cloudflared/<uuid>.json`.
3. Copy `services/queue/cloudflared.yml` to `~/.cloudflared/config.yml`, replace `TUNNEL_ID` with the
   UUID and the home directory with the account's; it forwards `queue.taucetiproject.org` to
   `http://127.0.0.1:8780` and answers 404 for anything else.
4. DNS: `cloudflared tunnel route dns tauceti-queue queue.taucetiproject.org` creates the proxied CNAME
   in the zone (or add it in the dashboard: CNAME `queue` to `<uuid>.cfargotunnel.com`, proxied).
5. Install and start the tunnel unit:
   `install -m 0644 ~/TauCetiQueue/services/queue/cloudflared-tauceti-queue.service ~/.config/systemd/user/ &&
   systemctl --user daemon-reload && systemctl --user enable --now cloudflared-tauceti-queue`
   (on a host without lingering, the `run-tunnel.sh` loop instead; Current deployment).
6. Check `curl -s https://queue.taucetiproject.org/healthz` reports the same `service.revision` as the
   local `/status`.
7. Create and activate the App webhook, as an organization owner on the App's settings page: tick
   Active, set the webhook URL to `https://queue.taucetiproject.org/webhook/github`, paste the secret
   from `github-webhook-secret` (content type JSON, SSL verification on) and save; then on "Permissions
   & events" subscribe to the eight events the registration script lists (pull request, pull request
   review, issue comment, status, check run, check suite, push, workflow run) and save. GitHub sends a
   `ping` on the first save. Confirm with the App's JWT: `GET /app/hook/config` shows the URL, `GET /app`
   lists the events, `GET /app/hook/deliveries` shows 2xx statuses, and `status.webhooks.received` on
   the service grows.

The tunnel carries only `/status`, the feeds, the webhook receiver and the admin endpoint; the admin
endpoint authenticates the operator's GitHub token and the webhook receiver verifies the HMAC, so no
Cloudflare Access policy is needed for increment 1.

## Backups

`services/queue/backup.sh` takes an online SQLite backup (the backup API through Python's stdlib, so
the service keeps running and the copy is a consistent snapshot), verifies it with `PRAGMA
integrity_check` and keeps the newest 14 copies in `~/.local/state/tauceti-queue/backups/`. The
installer enables `tauceti-queue-backup.timer`, which runs it daily at 03:17 UTC; on a host without
lingering the timer stays disabled and the backup runs by hand or from cron (Current deployment). Run it
by hand before any risky change:

```sh
~/TauCetiQueue/services/queue/backup.sh            # configured database, default directory
~/TauCetiQueue/services/queue/backup.sh --keep 30
```

Restore: stop the service, move the live `queue.sqlite3` (and its `-wal`/`-shm` files) aside, copy the
chosen backup to `queue.sqlite3`, start the service, and check `/status` shows the expected head and
approved inputs. Pending GitHub side effects are replayed by their idempotency keys; a restored store
that is older than GitHub reconciles on restart, which is the same path as recovery from a crash.

## Verification after a deploy

After every `install.sh` (and after any restart), in this order:

1. **Revision.** `uv run tauceti-queue status --expect-revision $(git -C ~/TauCetiQueue rev-parse HEAD)`
   exits 0; `service.started_at` is after the restart; `service.controller` is `running` (`unavailable`
   with a reason means a component is missing from this revision).
2. **The two feeds answer.** `curl -s https://queue.taucetiproject.org/api/observation | jq .schema`
   is `tauceti-bors.observation/v1` and `curl -s 'https://queue.taucetiproject.org/repositories/1/candidates?base=main' | jq .schema`
   is `tauceti-bors.candidates/v1`; both carry the same batches the status document shows.
3. **Webhook deliveries arrive.** The App's Advanced page lists recent deliveries with 2xx responses
   after the restart (redeliver one if the repository is quiet), and the delivery journal under
   `log_dir` records them; `journalctl --user -u tauceti-queue` shows them applied.
4. **A repair task is claimable.** `status.pending_task` names the task when the head has a source
   failure; a worker's claim (stream G) succeeds exactly once and status shows the claim with its
   worker and expiry. When no head is failing, the shadow task from the live Bors failure stands in.
5. **No new condition.** `status.conditions` is empty, or every entry predates the deploy.

## Runtime settings

The runtime controls change without a PR, through the admin endpoint:

```sh
uv run tauceti-queue settings show
uv run tauceti-queue settings set insertion_age_window 8
uv run tauceti-queue settings set repair_concurrency 2 --token "$(gh auth token)"
uv run tauceti-queue settings set approval_poll_interval_seconds 600
uv run tauceti-queue settings set insertion_probe_budget_seconds 10
```

`settings set` posts `{"<key>": <value>}` to `/admin/settings` with the operator's GitHub token
(`--token`, else `GITHUB_TOKEN`, else `gh auth token`) as a bearer token; the service checks
repository-admin permission on `TauCetiProject/TauCeti` with the same predicate as the security audit
(`tauceti_worker/security_audit.py::require_admin`). A change applies to new scheduling decisions,
is persisted in the store, and appears in `status.settings` immediately. Values are positive integers;
anything else is rejected before the request is sent. `--url` points the command at the public endpoint
when run from another machine. The full list with initial values is the settings table in
[controller.md](controller.md); `insertion_probe_budget_seconds` and `outbox_drain_budget_seconds`
(20 each) bound one pass's insertion probes and outbox pushes in wall-clock seconds (Monitoring below).

## Approval polling

Approvals arrive as TauCetiReview's `merge eligibility` check runs, reconciled on every `check_run`
webhook delivery. Behind the webhooks, `tauceti_queue/approval_poll.py` runs a reconciliation pass at
service start and every `approval_poll_interval_seconds` (default 900): it lists the open PRs with
the review App's check runs on their heads (one GraphQL call per 100 PRs, on the GraphQL quota) and
feeds through the same `eligibility.reconcile` path as a delivery exactly those whose decision the
service has not recorded: an eligible check at a head without a live input (a missed approval), an
unsafe review on a PR with one (a missed revocation), and a waiting input whose PR moved to another
head, closed or became a draft. A pass costs the listing (12 GraphQL calls for 1,200 open PRs) plus
at most four REST calls per difference found; it is skipped while the REST quota is below the loop's
reserve (200) and stops early when the quota runs out, leaving the rest to the next pass.

`status.approvals` reports `webhook` and `polled` (approvals discovered by deliveries and by the
poll), `revoked_by_poll`, and `last_poll` (the last pass: `at`, `reason`, `listed`, `with_check`,
`reconciled`, `approved`, `revoked`, `head_changed`, `closed`, `drafted`, `member_events`,
`skipped`, `stopped_early`, `errors`, `duration_seconds`). A growing `polled` while the App's
Advanced page shows no deliveries means the webhook is inactive or not reaching the service; a
growing `polled` beside healthy deliveries means deliveries are being lost. Run a pass by hand:

```sh
uv run tauceti-queue reconcile-approvals            # prints the pass summary
```

## Cutover

Preconditions (plan, "Transition to production"): the rehearsal on
`TauCetiProject/TauCetiQueueScratch` passed. There is no shadow period (operator decision,
2026-11-04): the live rehearsal on the scratch repository is the check before cutover. The cutover
itself is one setting change with two halves, made together, nothing drained:

1. Take a backup (`backup.sh`).
2. Leave the TauCeti repository variable `MERGE_BACKEND` at `bors`. The service is a drop-in for
   Bors's commands, dispatch, statuses and feeds, and every workflow caller only distinguishes
   `bors` from anything else (`docs/queue/github.md`, "MERGE_BACKEND"): `pr-build.yml` and
   `retry-ci-status.yml` verify scope approvals against the observation feed under `bors`,
   `queue-release.yml` is skipped under `bors`, and TauCetiReview's `merge-only.yml` and
   `merge-sweep.yml` publish `merge eligibility` checks and leave admission to the selected backend
   under `bors`. The cutover is therefore not a variable change: save Bors's two feeds first, then
   point the `bors.taucetiproject.org` hostname (the feeds the workflows read) and the TauCeti
   webhook deliveries at the service by suspending the `tau-ceti-bors` App installation, and enable
   the service's publication. Bors receives no further deliveries from then on; the service's own
   approval intake is authoritative.

   ```sh
   curl -s 'https://bors.taucetiproject.org/repositories/1/active-batches?base=main' > /tmp/bors-active-batches.json
   curl -s 'https://bors.taucetiproject.org/repositories/1/candidates?base=main' > /tmp/bors-candidates.json
   ```

3. Import Bors's state, once, immediately after suspending the Bors installation and before
   enabling publication (the service is still in shadow mode and admits nothing of its own yet):

   ```sh
   uv run tauceti-queue import-bors --feed /tmp/bors-active-batches.json \
       --candidates /tmp/bors-candidates.json --candidates ~/tmp/bors-journal/candidates-*.json --dry-run
   uv run tauceti-queue import-bors --feed /tmp/bors-active-batches.json \
       --candidates /tmp/bors-candidates.json --candidates ~/tmp/bors-journal/candidates-*.json
   ```

   The dry run prints what the import would do: every member of a waiting batch, sealed or not,
   becomes an approved input at its head with its original admission age (`original_admitted_at` from
   the candidate journals; the journal is bounded to the 500 most recent admissions and 100 most
   recent candidates, so pass every saved journal you have; a member no journal covers is aged from
   the import time and its evidence says `age_source: import-time`), its reviewer and priority, with
   evidence `{"source": "bors-import", "batch_id", "feed_observed_at", ...}`; the live PR is read
   first and a closed, draft, moved-head or unknown PR is refused and listed with its reason. The
   members of Bors's running head are recorded as the service's external head (`status.external_head`),
   not as inputs: the service inserts into its open batch but seals no head until that head lands,
   which it observes as Bors's `Source-PR`-less squash commits on `main`, as the head tip becoming an
   ancestor of `main`, or as Bors closing the members with its `[Merged by Bors]` title. Re-running the
   command imports nothing already present and reports the counts; if Bors's head is no longer running
   in a fresh feed (it landed, failed or was cancelled) the re-run releases its unlanded members to
   approved inputs with their ages, and `--release-head` does the same without a fresh feed. Note that
   a suspended Bors installation cannot land its head: if the head is still running at cutover, wait
   for its landing before suspending, or release it after.
4. Enable publication: set `publication_enabled = true` in `config.toml` and run
   `systemctl --user restart tauceti-queue`, then confirm `service.mode` is `production` in status.
   (A restart is needed because this flag is process configuration, not a runtime setting; it is the
   deliberate friction of the plan's "one setting change".) Status shows the imported inputs under
   `waiting`, Bors's head under `external_head` until it lands, and the first head when it seals.
5. Watch the first three heads as the plan's "First week" says, run the journal report daily, and
   watch `status.approvals.polled`: approvals the poll finds that no delivery brought mean the webhook
   path is not reaching the service.

### As executed on 2026-11-04

The sequence and times are in [status.md](status.md), "Cutover". Two facts the steps above did not
know: `bors.taucetiproject.org` was not a DNS record but a Workers Custom Domain of the `tauceti-bors`
Worker (Bors runs as a Cloudflare container behind it), and a custom domain takes precedence over any
DNS record, so the switch was `DELETE /accounts/ec2169bdf033f56b009956d4b64ba8ef/workers/domains/<id>`
(the wrangler OAuth token on chungus2 has that scope; its definition is saved as
`~/tmp/bors-journal/bors-worker-custom-domain-2026-11-04.json` there) followed by the CNAME `bors` to
the tunnel, which `cloudflared tunnel route dns` had already added. And the queue App had to be added to
`TauCetiProject/TauCeti`'s `merge-queue-main` ruleset bypass actors and to the classic protection's
pull-request bypass allowances (both via the REST API as an organization owner), or the fast-forward of
`main` is refused.

Rollback is the same two halves in reverse: restore the `tau-ceti-bors` installation and hostname (the
hostname by `POST /accounts/<account>/workers/domains` with the saved definition: `hostname`,
`service: tauceti-bors`, `environment: production`, `zone_id`) and
set `publication_enabled = false` (restart). The service returns to shadow mode with its records intact; Bors
resumes approval intake. Approved inputs the service holds at that moment are re-approved under Bors by
their authors or the review App; the service's observation feed lists them.

### Rebuilding the head

`POST /admin/rebuild-head` (bearer token of a repository admin, body `{}`) rebuilds the head's chain
from its members' accepted heads on the head's base, supersedes its open task, advances the version
and lets the next tick dispatch a fresh attempt. It is for a head whose commits must be regenerated
without a source change, for example after a chain tool fix changed the commit message format and the
head's CI failed on a consequence of the old format; it is refused while the head is claimed and when a
member carries agent edits, which a rebuild would discard. `POST /admin/retry-ci` stays the action for
exhausted infrastructure retries.

## Monitoring

Status (`/status`, schema `tauceti-queue.status/v1`) is the primary surface. The conditions from the
plan's "Production monitoring" stop the pipeline and page nothing; each becomes the first line of
`tauceti-queue status` and an entry in `status.conditions` with its start time and detail:

| Condition | Where it appears in status |
| --- | --- |
| stall on the head for longer than two stall intervals | `conditions[].condition = stall_over_two_intervals`; `stall.since`, `stall.phase`, `stall.silent_seconds`; the silent phase is marked in `head.ci.phases` with its last `updated_at` |
| inference hold older than an hour | `conditions[].condition = hold_over_one_hour`; `hold.since`, `hold.last_error` |
| a chain that fails validation twice for the same head | `conditions[].condition = chain_validation_repeated`; `head.batch`, the detail names the violation |
| a promotion that fails for a reason other than a canonical advance | `conditions[].condition = promotion_failed`; detail carries the GitHub response |
| divergence between the service's observation feed and its own records | `conditions[].condition = feed_divergence`; detail names the batch or member |

Also in status: the head's batch, base, tip, claim, CI run with `runner` and `host`, phase progress with
timestamps (`head.ci.phases`), the open batch's size and oldest member age (`open_batch`), approved
inputs waiting (`approved_waiting`), the pending task (`pending_task`), Bors's head awaited after the
cutover (`external_head`), approvals by source and the last poll (`approvals`), the service's running
revision and start time (`service.revision`, `service.started_at`), and the effective settings (`settings`).
"Which machine is this running on and is it moving" is answered by `head.ci.runner`, `head.ci.host` and
the newest `updated_at` among the phases; `tauceti-queue status` prints exactly these.

A reconciliation pass holds the controller lock, and `/status` waits at most two seconds for it
(`/healthz` half a second). When a pass runs longer, the endpoints answer with the snapshot the last
pass or operator action took, marked `snapshot: true` with its age in `stale_seconds`; a live read has
neither key. The feeds (`/repositories/1/active-batches`, `/repositories/1/candidates`) read the store
directly and never wait for the lock. A pass itself is bounded: insertion probes stop after
`insertion_probe_budget_seconds` and the outbox drain after `outbox_drain_budget_seconds` of wall time
(both 20 by default), the pass reports `insertion paused after N probes (budget)` or `outbox drain
paused with N entries pending (budget)` in `reasons`, and the next pass follows at once rather than
after the polling interval. A `stale_seconds` that keeps growing past the budgets plus the polling
interval means the loop is stuck in something unbudgeted (a git fetch, a GitHub call) and is worth a
look at the service log.

The journal report (`scripts/staging_journal_report.py`) runs daily and after every change against the
service's candidates feed and the Bors baseline window ([journal-report.md](journal-report.md)): landed
PRs per hour, head cycle time and its CI/repair split, repair rounds per landed head, repair time and
inference cost per task, shrink events, singleton builds (expected zero), claim collisions, PR waiting
age, inference holds and stall conditions, each beside its baseline value.

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
| `review_app_id` | `3947238` | identifies legacy automatic publications so policy mode can ignore late output; pre-cutover eligibility mode trusts this App |
| `admission_source` | `policy` | primary policy intake; `eligibility` is only the explicit pre-cutover configuration |
| `admission_lean_root` | `TauCeti` | trusted project root, converted to the allowed Lean path prefix |
| `admission_rubrics` | the ten standard rubrics | required review rubrics, comma separated; trusted service configuration |
| `admission_extra_files` | empty | extra allowed repository paths, comma separated, beyond the configured root file, Lake pins and bibliography |
| `[settings] insertion_age_window` | `16` | initial runtime control (plan, "Runtime controls") |
| `[settings] repair_concurrency` | `1` | initial runtime control |
| `[settings] stall_interval_seconds` | `900` | initial runtime control |
| `[settings] approval_poll_interval_seconds` | `900` | existing discovery cadence for missed-delivery reconciliation; pending policy work wakes on events or its actual retry deadline |

The `[settings]` values and `publication_enabled` are seeds: `serve` writes a file value into the
store (actor `configuration`) only when the store has no value for the key, or when the stored value is
itself an earlier seed and the file now says something else. A value set through the admin endpoint
(Runtime settings below) records the operator as its actor and is never replaced by the file, so once
the store exists the settings are changed with `settings set`, not by editing the file. Read-only
commands never write settings. Writing a setting's current value journals nothing. `check-config`
reads the store without writing and shows each runtime control's effective value with its source:
the store (and the actor that wrote it, beside the file's value) or the file; status reports the
effective values.

## Install and update

Both are the same command, run on the host as the service account, and both are live: the fleet keeps
admitting workers and CI, nothing is drained, and the only interruption is the service restart.
Normal source discovery recovers missed policy observations. GitHub does not automatically redeliver
failed webhooks; inspect failed deliveries after a restart and redeliver missed operator commands.

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

On a host without lingering (Current deployment, "Hosts without lingering") keep the units disabled
and use the deployed supervisor scripts described there. The installer enables and starts units
even with `--no-restart`, so it must not run beneath those supervisors.

Useful commands:

```sh
systemctl --user status tauceti-queue
journalctl --user -u tauceti-queue -f
uv run tauceti-queue status                     # one-screen summary from the local port
uv run tauceti-queue status --json | jq .service
uv run tauceti-queue status --url https://queue.taucetiproject.org   # through the tunnel
```

## Current deployment

Running on `chungus` since 2026-11-05 01:13 UTC, as the account `kim`. Service-owned policy intake
became authoritative on November 6 at 04:25 UTC with Worker revision `9d7180a97aac` and the pinned
TauCetiReview policy `7fd3a39e02c5`; subsequent live revisions are reported by `/status`. Both
`queue.taucetiproject.org` and `bors.taucetiproject.org` report `service.host` `chungus` since
01:13:33 UTC. The operator has no root on chungus and lingering is not enabled for the account
(`loginctl enable-linger` is denied). This is a fixed host constraint. The queue service and tunnel
retain their detached supervisors; since November 6 at 07:04 UTC, both repair workers run under
separate systemd user services, `tauceti-queue-repair.service` and `tauceti-queue-repair@2.service`.
Their supervisor is outside the workers' killable cgroups. These user services recover process
and worker-cgroup failures without root; they do not provide startup after a host reboot or loss
of the user manager. A dedicated host is planned; its availability and hostname remain outstanding.

The queue service and tunnel units stay **disabled** while their `nohup` supervisors run. The repair
units are enabled; do not start the old `run-worker*.sh` loops alongside them. Check
`systemctl --user list-units 'tauceti-queue*'` after any move or install. On 2026-11-05
`tauceti-queue.service` had been left enabled: it restarted about every 6 s (737 restarts), and each
attempt seeded the file's `[settings]` into the shared store before failing to bind port 8780, which
reverted the live `repair_concurrency` from 2 to 1 and left worker 2 refused every claim from 07:04 to
09:13 UTC. It was stopped and disabled at 09:24 UTC, and `config.toml` now carries
`repair_concurrency = 2` (backup `config.toml.bak-2026-11-05`). Since
https://github.com/TauCetiProject/TauCetiWorker/pull/544 the file only seeds settings (Configuration
above), so a stray start can no longer override a value the operator set.

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
| `~/TauCetiQueue-data/repair-clone`, `~/TauCetiQueue-data/repair`, `~/TauCetiQueue-data/repair-2` | shared repair clone and separate worker checkpoints; current logs are in the two repair units' journals |
| `~/.local/state/tauceti-queue/backups/` | online backups; daily timer active as of November 6, plus backups before deployments |
| `127.0.0.1:8780` | the listen address |
| `~/.cloudflared/cert.pem`, `config.yml`, `<tunnel id>.json` | the Cloudflare origin certificate, tunnel configuration and tunnel credentials, copied from tauchonki |
| `~/.config/systemd/user/tauceti-queue.service` | disabled while its detached supervisor owns the process |
| `~/.config/systemd/user/tauceti-queue-repair.service`, `tauceti-queue-repair@.service` | singleton and instance 2 enabled and active; each has its own worker identity and task root |
| `~/.config/systemd/user/tauceti-queue-backup.{service,timer}` | installed; timer enabled and active as of November 6 |

### Hosts without lingering

Without lingering, user services are not guaranteed to survive loss of the user manager or start
after reboot. They still provide normal crash and OOM recovery while the manager runs. Repair
workers must use those services: putting the worker and a shell restart loop in one login scope
lets `systemd-oomd` kill both together. `nohup` and a new session ID do not change cgroup ownership.
The queue and tunnel retain their existing detached deployment path below; do not run their
installer beneath those supervisors, even with `--no-restart`, because it starts a second owner.

```sh
cd ~/TauCetiQueue-data
nohup setsid ./run-service.sh >/dev/null 2>&1 &      # loops `uv run tauceti-queue serve`; log service.log
nohup setsid ./run-tunnel.sh >/dev/null 2>&1 &       # loops `cloudflared tunnel ... run`; log tunnel.log
systemctl --user start tauceti-queue-repair.service tauceti-queue-repair@2.service
pgrep -af 'run-service.sh|run-tunnel.sh|tauceti-queue serve|cloudflared tunnel|repair_task'
```

Consequences:

- **File-descriptor limit.** Detached processes do not get the units' `LimitNOFILE=65536`; they
  inherit the login's limit of 1,024, which a large repair exhausts. The supervisor scripts run
  `ulimit -n 65536` first. Repair services get that limit from their units.
- **No restart after a reboot.** After a reboot of chungus, or whenever `pgrep` shows a process
  missing, the operator or an agent starts the queue/tunnel loops and repair units as above.
  Worker process and cgroup kills recover automatically while the user manager remains available.
- **Backups.** The daily timer is enabled and active; its November 6 run succeeded at 03:19 UTC.
  Without lingering, this does not guarantee recovery after logout or reboot. Run
  `~/TauCetiQueue/services/queue/backup.sh` before any risky change and verify the timer after a
  host restart. Restore ordinary systemd supervision with lingering on the dedicated host.
- **Deploy** with the scripts in `~/TauCetiQueue-data`: `deploy-queue.sh` updates the service and
  both repair workers, `deploy-service.sh` the service only. These fast-forward the checkout, bake
  its revision into `_revision.py`, and send SIGTERM to the existing service process; the supervisor
  loop restarts it on the new revision with the same graceful shutdown as the unit. Verify as in "Verification after a deploy", reading `service.log` instead
  of `journalctl`.
- **Keep queue/tunnel ownership singular.** The queue installer enables and starts units even
  with `--no-restart`; do not run it beneath the detached supervisors. Use the deployed
  `deploy-queue.sh` / `deploy-service.sh` scripts, or fast-forward the checkout to a verified
  revision, bake `_revision.py`, and restart the existing supervisors. Confirm the service and
  queue/tunnel units remain disabled afterward. Repair units already own their workers, so
  `install-repair.sh` is appropriate for those units on this host.
- **Baked revision.** `tauceti_queue/_revision.py` is written by `install.sh` only. After a plain
  `git pull` in `~/TauCetiQueue`, rewrite it by hand before restarting, or the status page reports the
  old revision:
  `printf 'SHA = "%s"\nDIRTY = False\n' "$(git rev-parse HEAD)" > tauceti_queue/_revision.py`.
- **Repair workers.** The workers share the checkout, so a deploy that changes it needs each worker
  restarted (a running task is checkpointed and resumed). Use
  `systemctl --user restart tauceti-queue-repair.service` and, separately,
  `systemctl --user restart tauceti-queue-repair@2.service`. Do not use the old detached-worker
  scripts, which no longer own these processes.
  Never put a process pattern (`pkill -f`, `pgrep -f`, or the pattern as any argument) in an ssh
  command line: the pattern matches the ssh session's own shell, which on 2026-11-05 killed the
  operator's shell twice. Use the scripts above, or find the PIDs in a login shell on the host.
- **GitHub quota.** The operator's tooling, subagents and the workers' claims all spend the same
  GitHub user quota (5,000 requests per hour), so operator polling stays sparse: a quota exhausted by
  polling refuses the workers' identity lookups and stalls claims.
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
authorization is logged and retried on the next pass; the loop never exits for them. The worker's
admin check is cached for six hours in `admin-check.json` under the task root, and when GitHub cannot
answer it (rate limit, 5xx, network) the worker keeps the cached answer or, with none, claims anyway
and relies on the service's own admin enforcement; only the API saying the account is not an admin
stops it, and `--check` reports the cached verification's age. The worker runs
on the service host today; it only needs the service URL, so it can run on any host meeting the
requirements below (the service's `repair_concurrency` setting bounds how many hold a task at once).

Queue repair sessions using Claude (subscription or API) give Bash EOF on its default stdin.
The worker atomically writes `bash-env.sh` beside its persistent private Claude config and selects
it with the CLI's session `--settings` override. This intentionally overrides inherited, user and
project `CLAUDE_ENV_FILE` settings for these repair sessions, without editing those settings files.
Explicit input redirections, pipelines and heredocs still supply input normally. This works around
[Claude's compound-command stdin bug](https://github.com/anthropics/claude-code/issues/84424), where
a diagnostic filter without a filename could otherwise wait until the shell timeout. Codex and
other worker sessions keep their existing configuration. To run the native regression without
inference or credentials, use `TAUCETI_TEST_CLAUDE=claude uv run python tests/queue_claude_stdin.py`;
it drives the production repair launcher against a scripted local API, including cwd changes,
conflicting settings and a resumed session.

The service resolves each worker's bearer token through `GET /user`, which spends the token owner's
user quota, shared with any other tooling on that account. What GitHub answered is kept in the
store's `identity_cache` table (the SHA-256 fingerprint of the token, never the token, with the login
and the time GitHub last confirmed it; likewise each login's repository-admin answer) and loaded at
start, so a restart while that quota is exhausted does not lock the workers out. An answer is
revalidated in the background every five minutes once it is six hours old; while GitHub is rate
limited or unavailable the stored answer is served (logged once per token per hour) and lookups of
known identities pause until the limit's reset. A stored answer expires when GitHub has not
confirmed it for seven days, when GitHub answers that the token is invalid, or when an admin revokes
it with `POST /admin/revoke-identity` `{"login": "<login>"}` (bearer token of a repository admin),
which forgets that login's tokens and admin answers. Only a token the service has never resolved is
refused (401) during an outage. `/status` reports `identities`: `cached` (bearer identities),
`admin_answers_cached`, `github_lookups_rate_limited`, `github_lookups_retry_at` and
`last_lookup_error`. On the worker side a 401 or 5xx answer to a claim renewal does not abandon the
task: the agent keeps running and the renewal is retried until the claim's expiry, and a renewal that
succeeds before then continues the claim.

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
    managed install (2.1.318 on 2026-11-05) must come before any older copy on PATH. Likewise check
    `codex --version` against another fleet host's: an outdated Codex CLI rejects the default model
    (below).
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

Codex through a ChatGPT account serves the default Sol profile (`gpt-6.1-sol`, with its probe and
the `gpt-6-luna` fallback); no model pin is needed. A Codex CLI too old to know `gpt-6.1-sol` fails
instead: it logs "Model metadata for `gpt-6.1-sol` not found" and the server answers `model_not_found`
("The 'gpt-6.1-sol' model is not supported when using Codex with a ChatGPT account"), so the `codex`
provider fails and the task falls through to `api`. That error means an outdated CLI, not an account
limit: update it (`npm install -g @openai/codex@latest`) and restart the worker. On 2026-11-05 chungus
had 0.154.0, which failed this way; 0.166.0 answers normally on the same Pro account.

### Install and update

```sh
git clone https://github.com/TauCetiProject/TauCetiWorker.git ~/TauCetiQueue   # if not already present
~/TauCetiQueue/services/queue/install-repair.sh                   # origin/main
~/TauCetiQueue/services/queue/install-repair.sh --revision <sha>  # a pinned revision
~/TauCetiQueue/services/queue/install-repair.sh --instance 2      # another worker
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

An instance uses `tauceti-queue-repair@NAME.service` and `repair-environment-NAME`. When that file
is absent, the installer copies the singleton's provider/cache configuration and supplies a distinct
worker identity and task root. Both service forms execute the virtual environment's Python directly,
so `MainPID` matches `worker.json`. `Restart=always`, `OOMPolicy=kill` and disabled start-rate limiting
keep worker-cgroup kills recoverable without an external restart loop or a new memory cap.

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
stop it right after `worker.json` reports `idle`. For instance 2, use
`tauceti-queue-repair@2.service` in the same commands. Current repair workers use these units even
without lingering; the host limitation concerns user-manager loss and reboot startup.

If a head repair task has no live claim for `stall_interval_seconds`, `/status` reports
`head.stall.kind = "task_unclaimed"`; after two intervals it also raises an alert. The clock starts
from task creation or the last actual claim end. New admissions cannot reset it. The existing
monitoring projection publishes the same condition.

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
installer enables `tauceti-queue-backup.timer`, which runs it daily at 03:17 UTC. Without lingering,
an enabled user timer is not guaranteed to survive logout or recover after reboot; inspect its actual
state (Current deployment). Run the backup by hand before any risky change:

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
uv run tauceti-queue settings set lost_run_seconds 14400
uv run tauceti-queue settings set pre_rebase_enabled false
```

`settings set` posts `{"<key>": <value>}` to `/admin/settings` with the operator's GitHub token
(`--token`, else `GITHUB_TOKEN`, else `gh auth token`) as a bearer token; the service checks
repository-admin permission on `TauCetiProject/TauCeti` with the same predicate as the security audit
(`tauceti_worker/security_audit.py::require_admin`). A change applies to new scheduling decisions,
is persisted in the store, and appears in `status.settings` immediately. Every key of the controller's
`DEFAULT_SETTINGS` can be set, and the value is parsed by the type of its initial value (below);
anything else is rejected before the request is sent. `/admin/settings` itself takes one JSON object
of keys to values, refuses the whole request when any key is unknown, and checks each value's type.
`--url` points the command at the public endpoint when run from another machine. What each setting
means is in the settings table of [controller.md](controller.md); `insertion_probe_budget_seconds` and
`outbox_drain_budget_seconds` bound one pass's insertion probes and outbox pushes in wall-clock seconds
(Monitoring below).

| setting | initial | value |
| --- | --- | --- |
| `insertion_age_window` | `16` | integer, at least 1 |
| `open_batch_limit` | `2` | integer, at least 0 |
| `repair_concurrency` | `1` | integer, at least 1 |
| `stall_interval_seconds` | `900` | integer, at least 1 |
| `publication_enabled` | `false` | `true` or `false` |
| `claim_ttl_seconds` | `900` | integer, at least 0 |
| `dispatch_timeout_seconds` | `600` | integer, at least 0 |
| `lost_run_seconds` | `14400` | integer, at least 0 |
| `infrastructure_retry_limit` | `3` | integer, at least 0 |
| `infrastructure_retry_backoff_seconds` | `60` | integer, at least 0 |
| `inference_failure_threshold` | `3` | integer, at least 0 |
| `inference_hold_backoff_seconds` | `60` | integer, at least 0 |
| `inference_hold_backoff_max_seconds` | `3600` | integer, at least 0 |
| `outbox_retry_seconds` | `30` | integer, at least 0 |
| `outbox_retry_max_seconds` | `900` | integer, at least 0 |
| `polling_interval_seconds` | `60` | integer, at least 0 |
| `approval_poll_interval_seconds` | `900` | integer, at least 1 |
| `membership_label` | `bors-staging` | text |
| `repaired_label` | `repaired-in-queue` | text; empty disables |
| `insertion_probe_budget_seconds` | `20` | integer, at least 0 |
| `outbox_drain_budget_seconds` | `20` | integer, at least 0 |
| `prepare_open_batches` | `true` | `true` or `false` |
| `prepare_min_members` | `20` | integer, at least 0 |
| `prepare_quiet_seconds` | `600` | integer, at least 0 |
| `pre_rebase_enabled` | `true` | `true` or `false` |
| `prepare_pre_rebased` | `true` | `true` or `false` |


## Primary admission

The queue service evaluates published review evidence itself. `admission_source = "policy"`
uses the pure policy from the pinned TauCetiReview dependency, with explicit project configuration.
It runs no model and executes no PR code. The resulting input records the source review comment,
head, reviewed merge base and policy revision/digest as `admission_policy` evidence; it does not
claim to be an approval signed by the review App.

The existing approval-poll thread runs intake. Relevant authenticated webhooks persist dirty PRs
in `admission_work` in the existing SQLite database before acknowledgment. Reads and git fetches
happen outside the controller/database lock. Applying a result checks its generation and target
epoch, and commits the input and completed evaluation together. A newer event stays pending.

The same paginated discovery supplies PR, review-comment, label and commit-status inputs at
startup and every `approval_poll_interval_seconds`. Nested comment connections are completed.
Build changes invalidate decisions independently of PR `updated_at`; queue output and incidental
comments do not. A normal advance of the target that leaves the merge base unchanged creates no
new evaluation. Missing or incomplete source reads never imply approval or removal of unseen work.

Pending PRs have no invocation, PR-count, fixed-reserve or lifetime-attempt allowance. REST and
GraphQL waits retain their absolute server deadline. Intake does not sleep on an exhausted bucket;
other runnable work proceeds. Resident pagination buffers retain pages already read during a quota
wait. After a process restart the database still owns pending work and deadlines, and source
reconciliation restores observations. There are no admission workflow reruns or cache handoffs.

PRs waiting for the same API resource share its observed deadline in the existing database.
Advancing that deadline does not create another attempt or write for every waiting PR. Source
failures remain local to their PR, and urgent revocations can bypass background pacing while still
respecting actual server cooldowns. Startup restores these waits into the shared client, including
for newly observed PRs. Expected resource waits count as `deferred`, rather than poll errors.

An evaluation retains its FIFO place across resource waits, so completing a PR's source reads
does not require repeated rounds through the entire backlog. New input and genuine source
failures rejoin at the current time; a changing or failing PR therefore lets other work proceed.
The last-attempt timestamp and oldest-pending age remain separate diagnostics.

Background reads are paced across each resource's remaining window, using REST requests and
reported GraphQL point costs separately. Queue/CI calls, queued-review revocations and final
head/CI fences receive priority. Source preparation resumes at the next eligible read without
restarting from its first request. The final fence reads current CI independently of PR timestamps.
An approved input with a pending review change cannot be inserted until evaluation completes;
the same check protects a replay that was already in progress when the change arrived.

Manual withdrawal records a separate head/authorization fence, even before the first policy
evaluation. CI/configuration changes, incidental review edits and repeated polls cannot revive it.
A fresh published review round (or a new review comment) after withdrawal can supply fresh intent;
an explicit operator approval can too. Already-inserted membership remains governed by the normal
queue rules. Late legacy eligibility checks and automatic review-App commands are ignored in
policy mode. The old eligibility consumer is available only through an explicit pre-cutover
configuration; there is no automatic change of admission owner.

At cutover, legacy withdrawals retain their recorded journal or rejection-evidence time. A new
review comment created after that boundary can restore approval. If legacy records do not retain
the earlier review authorization, an edit to an old comment cannot prove fresh intent; a new
comment or explicit operator approval is required. Missing withdrawal history is held conservatively.

`status.admission` reports the active source, policy digest, pending count, oldest pending time,
and pending PRs with attempt time, retry deadline, resource and last error. The existing
`approvals.last_poll` field carries the intake summary. Successful decisions are journaled as
`admission-evaluated`, including their reason; `admission_evaluations` and `admission_approvals`
count completed work. `tauceti-queue reconcile-approvals` wakes this same intake path for diagnosis;
routine progress does not require that command.

To switch an existing eligibility deployment, back up the database, verify its process supervisor,
deploy the service live with `admission_source = "policy"`, and verify that source and revision in
status. Then retire TauCeti's separate `auto-merge`/`merge-sweep` workflow execution and calls to
merge-only evaluation, leaving review publication and ordinary CI enabled. The service ignores
in-flight old publications from the moment it switches. Preserve worker/CI admissions throughout.
Verify pending work, useful admissions/landings and restart recovery from the same database.

Inference failure never transfers this repository back to legacy Bors. Deterministic intake
continues to use valid existing review evidence; repair-dependent tasks retain membership and
source under the existing inference hold and retry automatically when a provider returns.
`MERGE_BACKEND=bors`, Bors-compatible commands, feed schemas and hostnames remain compatibility
surfaces for this engine. TODO: make this same queue operate without inference using splitting;
the design is deferred and hold-and-retry is interim behavior.

## Original Bors cutover

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
   (The file's value is a seed: if an operator has set `publication_enabled` through
   `settings set`, that value stands and is changed the same way.)
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

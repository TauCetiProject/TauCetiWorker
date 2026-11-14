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

For the deployed **primary intake**, narrow the App subscriptions in
[Permissions & events](https://github.com/organizations/TauCetiProject/settings/apps/tauceti-queue/permissions):
first set **Checks** to **Read-only** (the live registration retained write access for the rehearsal
tool, while production only reads checks). GitHub automatically subscribes Apps with Checks write
access to check events. Then keep `pull_request`, `issue_comment`, `pull_request_review`, `push`, and
`status`; deselect `check_run`, `check_suite`, and `workflow_run`. This prevents future rehearsals
from publishing checks until write access is explicitly restored. The registration manifest above also supports legacy
approval intake, which still needs the review App's check notifications. Primary intake uses published
scoreboards and commit statuses; candidate CI is also polled. GitHub's App webhook REST configuration
can change the URL/secret/content type, but does not expose the event subscriptions. Event types are
selected in the App settings; the receiver filters branches, status contexts, labels and comment bodies.

On November 9, those three CI event types accounted for about 94% of processed events between
10:00 and noon AEDT. In primary mode the receiver now acknowledges them as `ignored` immediately
after signature and repository validation, without admission work, a controller lock, journal writes,
or GitHub I/O. It similarly filters branch pushes, irrelevant statuses/labels/title edits and prose.
Relevant label changes, PR lifecycle events, scoreboard edits/deletions, human Bors commands and
main pushes retain their existing handling. The `/status` webhook `ignored` counter exposes the drop.
Controller lock release wakes one eligible waiter, preserving worker endpoint priority without
broadcasting to every waiting webhook thread.

Meaningful events still run synchronously. Further overload protection should persist those events
before acknowledgement, process them with bounded workers and expose pending count/age, processing
latency and failures independently of controller health. GitHub does **not** automatically redeliver
failed webhooks. Polling reconciles missed state notifications; failed operator commands need explicit
redelivery or resubmission. Do not depend on a 5xx response to cause a retry.

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
  service survives logout and reboots. The current host is `chungus2`; lingering is not enabled
  there, so the reboot-startup guarantee remains postponed.
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
| `admission_rubrics` | the ten standard rubrics | required review rubrics, comma separated; trusted service configuration. A rubric the pinned review policy is phasing in (`PHASED_RUBRICS`) is also required, of PRs above its cutoff for their source repository: GitHub and Forgejo number their PRs separately, so each has its own |
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

The current deployment uses systemd user units despite lacking lingering: process recovery works
while the user manager runs, but reboot startup is not guaranteed. Follow the
[current deployment procedure](#supervision-and-deployment); retired detached launchers must not run
alongside the units.

Useful commands:

```sh
systemctl --user status tauceti-queue
journalctl --user -u tauceti-queue -f
uv run tauceti-queue status                     # one-screen summary from the local port
uv run tauceti-queue status --json | jq .service
uv run tauceti-queue status --url https://queue.taucetiproject.org   # through the tunnel
```

## Current deployment

The controller, SQLite store, content clone, public tunnel, monitoring publisher,
history timer and backup timer run as `kim` on **tauchonki2** since November 10.
Both public hostnames, `queue.taucetiproject.org` and `bors.taucetiproject.org`,
terminate at the existing tunnel identity on that host. Canonical `main`, publication,
automatic admission and the persisted three-worker concurrency policy are unchanged.

Repair workers transfer individually at fresh idle heartbeats with no live claims.
During the transition, existing claims continue on chungus2 and renew against the
same public queue URL; replacement workers use `@tauchonki2` identities. A finite
handoff service on chungus2 stops each old worker only when idle, starts its
counterpart on tauchonki2, mirrors retained task observations, and collects source
worktrees against a fresh read-only snapshot of the destination database. Never
restart a retired source worker to repair apparent inactivity without checking
current claims and the handoff receipt first.

New staging builds are pinned by repository variable `STAGING_RUNS_ON` to
`fleet-74773fc7f54d4877add11d80113fe340`, tauchonki2's reserved staging-only runner.
Its CPU weight is 10000 versus 100 for ordinary PR runners. Staging compiler
memory-admission sleeps are disabled; ordinary CI retains its admission policy.
Physical VM/cgroup limits still apply. Already-running staging builds finish on
their original host; neither routing changes nor controller relocation moves them.

The November 10 cutover stopped the old writer and tunnel, transferred a consistent
SQLite backup with verified integrity and table counts, synchronized final source
refs and publisher state, and verified local health before enabling the destination
tunnel. Public service returned after approximately 40 seconds. Active staging and
repair processes retained their run identities. The source controller, tunnel,
monitoring and backup units are disabled and masked to prevent competing writers
or stale publication. The App delivery journal covered the cutover window: all eight
relevant missed deliveries were confirmed successful; 1,417 failed notifications
were discarded by the existing primary-intake filter. Migration evidence and final source snapshot are under
`~/TauCetiQueue-data/migration-tauchonki2-20261110/` on chungus2; the destination
holds its runtime checks and transfer receipts under the same path.

All active processes use systemd user units. There are no detached controller/tunnel supervisors
on the destination. The retired units on chungus are masked and its `run-service.sh`,
`run-tunnel.sh`, `restart-service.sh` and deployment helpers refuse execution. Original files,
final database backup and migration evidence remain under
`~/TauCetiQueue-data/controller-migration-to-chungus2-20261108/` on chungus. Older repair worktrees
and agent logs remain there as history; task JSON needed for reporting was copied to chungus2.
Do not resurrect any retired unit while the new controller is authoritative.

Tauchonki2 has `Linger=yes`; the enabled user units and timers start with its user manager
after reboot. Verify the public endpoint, backup, monitoring and worker claims after recovery.

Production repair admission uses `repair_concurrency=3` and `background_start_limit=2`.
Count all live claims when starting a task: a non-head task can increase the total to
at most two, while a head task can increase it to three. Keep head priority and one
owner per batch. Two non-head preparations can therefore run while no head needs
repair, with a worker available when a head task appears. Claims are still pulled by
idle workers on their polling intervals; this rule removes the wait for another
repair to finish.

Run three interchangeable processes with distinct identities and task roots. The
additional user unit is `tauceti-queue-repair@3.service`, using the private
`repair-environment-3` and `~/TauCetiQueue-data/repair-3`. Add that root to both repair
worktree cleanup and public monitoring (and historical telemetry) when enabling it.
Existing active workers need not restart for controller admission changes.

Paths on tauchonki2, where they differ from the default layout:

| Path | Content |
| --- | --- |
| `~/TauCetiQueue` | shared code checkout for controller and repair workers at the installed revision |
| `~/.config/tauceti-queue/config.toml` | `host = "tauchonki2"`, existing App/review/admission configuration and data paths |
| `~/.config/tauceti-queue/environment` | controller overrides, including `SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt` for NixOS TLS trust |
| `~/TauCetiQueue-data/clone` | controller content clone, including retained private source refs |
| `~/TauCetiQueue-data/queue.sqlite3` (+ `-wal`, `-shm`) | authoritative queue database |
| `~/TauCetiQueue-data/logs` | service-owned files; process logs use `journalctl --user -u tauceti-queue` |
| `~/TauCetiQueue-data/repair-clone`, `repair`, `repair-2`, `repair-3` | separate repair Git clone and worker task/cache roots; active tasks use private writable dependencies |
| `~/TauCetiQueue-data/repair-shared/lake-cache` | one shared Lake cache for all repair workers, seeded from the source host |
| `~/TauCetiQueue-data/monitoring/` | publisher identity/epoch/delta state, private token, prepared observations and pinned exporter release |
| `~/TauCetiQueue-data/reports/` | retained pilot inputs and Bors baseline used by the history timer |
| `~/.local/state/tauceti-queue/backups/` | daily and pre-deployment SQLite backups; an immediate destination backup was verified |
| `~/.config/tauceti-queue/tunnel/config.yml`, `<tunnel id>.json` | existing tunnel identity and configuration; DNS did not change |
| `~/TauCetiQueue-data/migration-tauchonki2-20261110/` | cutover checks, runtime setup and worker-handoff evidence |

### Supervision and deployment

On tauchonki2:

```sh
systemctl --user status tauceti-queue cloudflared-tauceti-queue
systemctl --user status tauceti-queue-repair tauceti-queue-repair@2 tauceti-queue-repair@3
systemctl --user status tauceti-queue-monitoring
systemctl --user list-timers 'tauceti-queue*'
```

The controller and repair units use `LimitNOFILE=65536`. Their process supervisors remain outside
worker OOM victim groups. Monitoring/history units retain their previous limits and run independently
of scheduling. The publisher reads local worker roots directly. During the November 10 transition,
retained source observations are mirrored under `legacy-chungus2/`; remote PID liveness
is unverified. The finite handoff service refreshes those observations until each old worker stops.

Before an update, take an online backup with `services/queue/backup.sh`, verify the target revision
and fast-forward the checkout. Use the service installer for controller deployment or explicitly
bake `_revision.py` and restart `tauceti-queue.service`. Since the checkout is shared, an update
that changes repair code also needs each repair worker restarted through its unit; a running task
is checkpointed and resumed. Keep ordinary admissions enabled. Verify public revision, accepted
members, claims, CI identity and useful progress after the update; do not use the retired shell
helpers on chungus.

A plain `git pull` does not refresh the baked revision. Before a manual service restart:

```sh
cd ~/TauCetiQueue
printf 'SHA = "%s"\nDIRTY = False\n' "$(git rev-parse HEAD)" > tauceti_queue/_revision.py
systemctl --user restart tauceti-queue
```

Stop a service through its unit rather than killing arbitrary process patterns. On a host without
lingering, the units/timers may need starting after a reboot/user-manager restart. Confirm public
health and backup/monitoring freshness afterward. Never run two controller writers or two publishers
for the same database/source identity.

### Hosts without lingering

Without lingering, units provide normal crash/OOM recovery only while the user manager runs.
After reboot or manager loss, start the units and timers and verify endpoint and observation
freshness. Keep repair supervisors outside the worker cgroups: `nohup` does not prevent a login
scope OOM kill from taking a worker and its restart loop together.

GitHub user API quota is shared with other operator tooling and repair identity checks; keep
operator polling sparse so it does not stall worker claims.

### Controller relocation on November 8

Preparation copied the code at the running revision, App/tunnel credentials, Git clone and pinned
monitoring release. Configuration, Python TLS trust, App authentication and tunnel rules were
verified before the outage. The old controller stopped gracefully; a final SQLite backup passed
integrity checking. Every table count, active batch/base/tip/version and retained Git object was
verified before starting the destination. All 4,363 existing member records and effective runtime
settings matched afterward. Repair workers retained their processes and identities.

The public endpoint was available on chungus2 approximately 73 seconds after cutoff. The tunnel
UUID stayed `be28d750-6092-4526-8aed-76a0d0bf7c66`; its old connector stopped before the destination
connector started. Monitoring transferred its persisted source/epoch state, and backups were
started and verified on the destination. GitHub's App delivery journal was audited across the
outage: all 159 failed deliveries were recovered with the App's redelivery API and matched to
successful delivery GUIDs. Polling alone does not recover every comment/review event. Preserve the recovery receipts with migration
state; ambiguous POST responses require read reconciliation, not blind retries.

Head 388 was already held on an import-header CI preflight failure before relocation. Preserving
that head is not evidence of a fresh green build or landing, and the relocation did not change
its verdict or retry policy. New controller journal/admission activity and fresh local worker
observations establish continued operation; the existing CI failure requires separate diagnosis.

### Future relocation or rollback

A future host move follows the same ownership transfer: prepare tools, credentials, code, TLS,
content objects and monitoring release first; then stop the source writer and publisher, take a
consistent final database backup, and copy it with the final Git refs and publisher state. Validate
members/settings and source objects before starting the new controller. Switch the existing tunnel
only after local health is verified. Audit/redeliver outage webhooks, run a destination backup and
verify monitoring/history and worker claims.

Rollback to the previous host requires transferring the **current** tauchonki2 database and source/publisher
state after stopping its authoritative services. The old pre-move backup is historical, not a safe
replacement for work accepted since cutover. Restore/unmask the source units and configuration only
once destination ownership has ended. Retained credentials and originals make rollback possible;
old data is kept until an operator authorizes cleanup.

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
    The `claude` binary first on PATH must be recent enough for the configured model; the managed
    install must come before any older copy on PATH. Likewise check
    `codex --version` against another fleet host's: an outdated Codex CLI rejects the default model
    (below).
    Leave a provider out of the chain when the host has no credential for it; `--check` reports each
    one named in the chain.
- **Worker cache** (optional): `TAUCETI_REPAIR_CACHE_URL`, `TAUCETI_REPAIR_CACHE_UPLOAD_URL` and
  `TAUCETI_REPAIR_CACHE_KEY_FILE` ([repair-task.md](repair-task.md), "Repair cache publication").
  The bucket and `repair-cache.taucetiproject.org` are configured on chungus2, including a private
  upload credential; publication uses the existing worker configuration.
- **Parallel agents.** `TAUCETI_QUEUE_PARALLEL_AGENTS` (default 3) bounds the concurrent agents within
  one task ([repair-task.md](repair-task.md), "Parallel agents within one task").
- The content clone named by `TAUCETI_QUEUE_CLONE` (the installer creates it).

### Terminal repair worktree cleanup

On the colocated controller/repair host, install the lifecycle collector after deploying the
worker/publisher ownership leases:

```sh
cd ~/TauCetiQueue
services/queue/install-repair-cleanup.sh \
  --database ~/TauCetiQueue-data/queue.sqlite3 \
  --clone ~/TauCetiQueue-data/repair-clone \
  --root ~/TauCetiQueue-data/repair \
  --root ~/TauCetiQueue-data/repair-2 \
  --root ~/TauCetiQueue-data/repair-3
```

`tauceti-queue-repair-cleanup.timer` runs independently of the workers, on startup and every minute
after the preceding collection ends. It reads the local authoritative SQLite database without
writing it. Landed/removed batches and submitted/superseded tasks are eligible, including worktrees
left by failed attempts and `task-<id>-group-*` parallel worktrees. The collector removes their Git
worktree registrations and build/dependency directories; task JSON, logs, retained refs and artifact
caches remain. `--keep-worktree` does not exempt terminal work from this collector.

Each worker attempt and background publisher holds a shared per-task filesystem lease. Collection
requires the exclusive lease and rechecks lifecycle, checkpoint identity and live claims before
removing anything. It defers active owners and live claims, allows a bounded two-minute publisher
startup handoff, and retries deferred trees on subsequent runs. A killed owner releases its kernel
lock automatically; stale publisher records cannot retain trees indefinitely. Lease files live
outside the trees and are never unlinked. Unknown/mismatched task identity, unreadable state and
symlinked roots are not deletion authority. Do not point a collector at a different queue's database.

```sh
# Preview eligible/busy trees (no worktrees removed):
.venv/bin/python -m tauceti_queue.repair_cleanup \
  --config ~/.config/tauceti-queue/repair-cleanup.json
systemctl --user status tauceti-queue-repair-cleanup.timer
journalctl --user -u tauceti-queue-repair-cleanup -n 20
```

Errors fail the oneshot and remain visible in its journal; the timer retries on the next pass.
This requires the database and worker roots on the same host. Remote-worker lifecycle collection
needs an authoritative remote protocol rather than inferring terminality from missing status rows.
The collector does not bound the separate artifact cache or implement a disk-space admission gate.

### Repair execution on chungus2

The November 7 move changed only repair execution. The two idle source workers were stopped and
disabled, with no unfinished checkpoint or active claim to transfer; old worktrees, outcomes and
logs remain on chungus. The replacement workers run revision `6348a0116c2c` from `~/TauCetiQueue`
on chungus2 and use its own GitHub administrator login and subscription credentials. The existing
API-provider and repair-cache upload configuration were copied privately, without changing
provider order, pacing, queue concurrency, admission or Fleet settings. The unit environment sets
`SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt` for Python's TLS verification on this NixOS host.

Live state on chungus2 is under `~/TauCetiQueue-data/repair{,-2,-3}`, with the common content clone at
`repair-clone`. To avoid duplicating old caches on a nearly full filesystem, the two private cache
directories were initially seeded with hardlinks from one fresh public-main fetch. Dependency
sources and built outputs at `mathlib-packages` were hardlinked from a compatible local checkout;
every package revision was checked against the current manifest, including Mathlib
`1b1ccf4b088b32fa3f23f2de1127433254a81e1e`. That initial read-only seed configuration was retired on
November 8: stock Lake attempts to update artifact permissions/hash metadata even while replaying
existing dependency outputs, which fails inside the sandbox
(`failed to cache artifact: read-only file system`). All three environments now leave
`TAUCETI_QUEUE_SEED_PACKAGES` empty and use the normal private dependency preparation path.
Do not restore the shared read-only symlink without validating a real `lake build`, including
artifact-cache replay. A direct Lean compilation alone does not test this path.

The failed task-56 worktree on worker 2 received an independent XFS reflink copy of its dependencies;
a stock-Lake sandbox build of `Batteries.CodeAction.Deprecated` passed afterward. The task was
reclaimed by worker 1 with normal private dependency preparation, and its streaming build produced
successful TauCeti compilations without the read-only artifact error. The dependency seed and
original environment files remain retained for diagnosis. Builds keep stock Lean `v4.36.0-rc2`,
the offline sandbox, Lake artifact caching and the existing pressure gate. Check disk before large
repairs and retain the existing cache-pruning discipline; private dependencies consume additional
space.

The temporary SSH observation mirror used between the November 7 repair move and the November 8
controller move is now retired. The local monitoring publisher reads `repair`, `repair-2` and `repair-3`
directly on chungus2, including original worker PIDs, task progress and build-log observations.
Historical task/outcome JSON was copied from chungus; larger old transcripts and worktrees remain
retained there. The masked repair units and archived launchers on chungus are not a second active
execution path. A repair-only rollback must preserve any new claimed work and explicitly restore
its source state; a controller rollback must transfer the current database as described above.

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
stop it right after `worker.json` reports `idle`. For instances 2 and 3, use
`tauceti-queue-repair@2.service` and `tauceti-queue-repair@3.service` in the same commands. Current repair workers use these units even
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
   (the current deployment uses this unit even without lingering; see Current deployment).
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
uv run tauceti-queue settings set repair_concurrency 3 --token "$(gh auth token)"
uv run tauceti-queue settings set background_start_limit 2
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
| `background_start_limit` | `2` | integer, at least 0; 0 disables new background claims |
| `stall_interval_seconds` | `900` | integer, at least 1 |
| `publication_enabled` | `false` | `true` or `false` |
| `claim_ttl_seconds` | `900` | integer, at least 0 |
| `dispatch_timeout_seconds` | `600` | integer, at least 0 |
| `lost_run_seconds` | `14400` | integer, at least 0 |
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

An infrastructure-only PR may instead carry a repository admin's explicit `bors r+`
or `bors merge` approval. When it has no current AI scoreboard, intake preserves
that recorded approval only while its exact head and merge base still match and
the approving account ID still has effective admin permission. Every changed path
must be outside the configured mathematics prefix and automatic allowed files;
mixed mathematics/infrastructure and pin-only PRs retain ordinary machine review.
A current negative scoreboard still rejects the PR. Permission-read failures leave
evaluation pending and prevent insertion. The decision records the human approval
and permission-check time, and the scope observation exposes its original evidence.
All staging build, audit, pin and scope checks remain required before publication.
An approval previously cancelled by policy needs a fresh operator command after repair.

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

### Forgejo Stage 1 authority transfer

The [Stage 1 cutover runbook](../forgejo-stage1-cutover.md) defines a separately
authorized transfer to human-operated Forgejo development. While that authority
transfer is active, keep the old GitHub queue's persisted `publication_enabled`
setting false and its service disabled. Do not use the Bors rollback above to
restore a second GitHub merge writer.

`services/queue/install.sh` enables and starts the controller even with
`--no-restart`. Do not run it on a host fenced for Forgejo cutover without the
explicit authority decision. Preserve the host's deployment notice, old ledger,
prepared work, and configured settings; a file seed or fresh database must not
silently restore publication. The GitHub mirror can share the old queue App
identity, so a branch exception alone does not distinguish those processes.

`services/queue/install-repair.sh` also enables its unit with `--no-restart`.
Keep it out of updates to a fenced host without the explicit authority decision.
Record the identity behind each repair token and fallback login; retain any
repository-admin credential only in the named administrative profile, outside
retired unattended repair runtimes. Preserve the corresponding recovery records.

This is a cutover condition, not an instruction to stop current production during
preparation. Keep normal worker/CI admissions enabled until the authorized
project-specific authority switch.

### Rebuilding the head

`POST /admin/rebuild-head` (bearer token of a repository admin, body `{}`) rebuilds the head's chain
from its members' accepted heads on the head's base, supersedes its open task, advances the version
and lets the next tick dispatch a fresh attempt. It is for a head whose commits must be regenerated
without a source change, for example after a chain tool fix changed the commit message format and the
head's CI failed on a consequence of the old format; it is refused while the head is claimed and when a
member carries agent edits, which a rebuild would discard. `POST /admin/retry-ci` stays the action for
persistent infrastructure failures; automatic retries continue with capped backoff.

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

### Repair cache retention

Set `TAUCETI_QUEUE_CACHE_ROOT` identically for all colocated repair workers, for example
`/home/operator/TauCetiQueue-data/repair-shared`, and pass that path to
`services/queue/install-repair-cleanup.sh --cache-root PATH` along with every worker root.
They share one `lake-cache` and its ownership lease, while credentials/configuration,
worktrees, logs and task checkpoints stay in their separate worker roots. Tasks persist
their selected cache root so a restart/configuration change cannot move an active attempt.
Publishers receive that exact root in their spec. Fetches use disposable per-fetch
views seeded by hardlinks to shared archives, then atomically transfer verified
metadata to the common pool; this isolates stock Lake's fixed download temporary
filenames without serializing downloads or builds. Views are removed on exit, and
idle retention also removes scratch directories left by crashed fetches.

The minute-by-minute cleanup timer migrates idle legacy pools into the common pool
using atomic hardlinks without overwriting existing destination names, then removes
the redundant private directories. Busy legacy attempts keep their old pools until
completion; migration retries automatically without changing admissions. The timer
also collects idle `lake-cache` pools.
Each pool retains at most two distinct snapshots per package from the last seven days,
within a total 32 GiB artifact budget (one budget for the shared pool). Active attempts
can temporarily exceed the budget; collection waits for their leases. Recent complete
dependency output records without revision maps share this budget. Successful local publication manifests count as
snapshots, including publications made before retention was deployed. If a snapshot
would exceed the budget, it is not retained; a future task can fetch it from the cache service.

Retention follows both compressed archives and every expanded native output facet,
removing obsolete output indexes and artifact aliases (including hardlinks). External
hardlinks remain valid; reported freed bytes count only final links. Diagnostic logs,
task checkpoints and source Git references remain available.

Worker attempts and background publishers hold a shared root cache lease; collection
takes it exclusively without waiting. Cleanup also runs before a new attempt
when its pool has no worktrees. Shared-pool collection requires all participating
attempts and publishers to be idle; it never interrupts them to enforce the budget.
Any remaining worktree, live local task claim or
recent publisher handoff also prevents collection, protecting older worker processes.
Unknown metadata or symlinks fail closed before any cache files are removed. Cleanup
prints per-pool retained bytes, removed artifacts/indexes and physically freed bytes.
The usual cleanup command without `--apply` previews the plan.

### Focused repair checks and worker verification

For warm `repair` and `prepare` attempts with worker audits enabled, the agent repairs
located findings and runs `.tauceti-repair/check-findings`. This helper compiles the
finding modules and the repair's source delta from the failing candidate, then runs
only the reported linters with the existing pinned native driver, or the pinned
ownership-only checker for duplicate declarations. It records its advisory scope and
timings in `tasks/<id>/logs/targeted-*/result.json`. Unavailable or unsupported checks
are reported explicitly; they do not authorize submission.

The agent can also run `.tauceti-repair/build <Module.Name> --dependents`. Its full-suite
helper is disabled in this mode: the worker owns the whole-library build, audits and
environment lint after the agent returns. Remaining failures go back to the agent as
diagnostics. Rebase/cold attempts and configurations without worker verification keep
their existing validation contract. No agent-reported or cached verdict replaces the
worker's final verification.

Verification records include wall-clock costs for tools setup, script materialization,
driver preparation, build, audits and lint. Existing `audit-import`, `audit-duplicates`
and `audit-axioms` timestamps are preserved as `logs/audit-phases.tsv` and summarized
in `logs/checks-timing.json` and the outcome's verification record. Incomplete intervals
are marked incomplete; missing telemetry never changes the checker verdict. Phase
records survive worktree collection, so further checker optimization can be guided by
measurements rather than inferred from one combined duration.

### Overlap repair publication with verification

With the default `TAUCETI_REPAIR_PUBLISH=after`, a green whole-library build freezes its
output map and archive hardlinks before the remaining audit/lint checks start. A normal
background publisher uploads the snapshot using stock `lake cache put-staged --rev=...`;
it does not depend on the live source tree or read a later checkout's Git revision.
This applies to worker verification, clean preparation and the rebase agent's full-build
helper. Snapshots require platform-independent root-package outputs and a fixed toolchain
name; unsupported layouts retain the existing late publisher.

Early publication does not pass an audit or authorize submission. Failed checks still
go back to repair, and staging starts immediately after adoption without waiting for
upload completion. An exact-revision early upload is reused; a changed revision, failed
upload or unavailable snapshot falls back to the existing after-submission path. Rebase
prompts ask agents to finalize fixups and message explanations before their full build,
so the snapshot names the revision eventually submitted.

Early logs and retained output manifests live in `tasks/<id>/logs/publications/<revision>/`.
Frozen payloads live in `worktrees/task-<id>-publish-<revision>` and are removed after upload.
They hold the same task/cache leases, and the existing terminal-task cleanup also removes
crashed publishers' snapshots. Cache retention follows successful early manifests.
The upload receives the key only in its own environment. There is no staging polling,
waiter or cross-service coordination. This gives uploads a head start; it does not
guarantee a hit if publication remains slower than verification and staging setup.

The [mixed-source queue configuration](mixed-sources.md) adds an explicit,
default-disabled Forgejo input to the existing queue. It documents observation,
admission, trusted native reviews, CI roles and the serialized primary change.

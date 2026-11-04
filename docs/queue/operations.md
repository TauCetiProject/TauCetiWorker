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
authenticating with the stored key and listing the installation's repositories). Its webhook address is
`https://queue.taucetiproject.org/webhook/github`, registered inactive until the service host and that
Cloudflare hostname exist; the deployment stream uses the same name.

Registration is prefilled by:

```sh
uv run python scripts/tauceti_queue_github_app.py url
```

Open the printed URL as an organization owner, set a webhook secret in the form (GitHub does not
accept one through the URL), and create the App. The URL requests exactly these repository
permissions: contents write, pull requests write, issues write, checks read, statuses write,
actions write, workflows write, metadata read; and subscribes to these events: pull request,
pull request review, issue comment, status, check run, check suite, push, workflow run. Pass
`--webhook-url https://<host>/webhook/github` once the service host exists; until then the webhook
is registered inactive and switched on from the App settings later.

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

Useful commands:

```sh
systemctl --user status tauceti-queue
journalctl --user -u tauceti-queue -f
uv run tauceti-queue status                     # one-screen summary from the local port
uv run tauceti-queue status --json | jq .service
uv run tauceti-queue status --url https://queue.taucetiproject.org   # through the tunnel
```

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
   systemctl --user daemon-reload && systemctl --user enable --now cloudflared-tauceti-queue`.
6. Check `curl -s https://queue.taucetiproject.org/healthz` reports the same `service.revision` as the
   local `/status`.
7. Activate the App webhook: on the App's settings page set the webhook URL to
   `https://queue.taucetiproject.org/webhook/github` (already recorded there, inactive) and tick Active;
   the secret is the one stored in `github-webhook-secret`.

The tunnel carries only `/status`, the feeds, the webhook receiver and the admin endpoint; the admin
endpoint authenticates the operator's GitHub token and the webhook receiver verifies the HMAC, so no
Cloudflare Access policy is needed for increment 1.

## Backups

`services/queue/backup.sh` takes an online SQLite backup (the backup API through Python's stdlib, so
the service keeps running and the copy is a consistent snapshot), verifies it with `PRAGMA
integrity_check` and keeps the newest 14 copies in `~/.local/state/tauceti-queue/backups/`. The
installer enables `tauceti-queue-backup.timer`, which runs it daily at 03:17 UTC. Run it by hand before
any risky change:

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

The three runtime controls change without a PR, through the admin endpoint:

```sh
uv run tauceti-queue settings show
uv run tauceti-queue settings set insertion_age_window 8
uv run tauceti-queue settings set repair_concurrency 2 --token "$(gh auth token)"
```

`settings set` posts `{"<key>": <value>}` to `/admin/settings` with the operator's GitHub token
(`--token`, else `GITHUB_TOKEN`, else `gh auth token`) as a bearer token; the service checks
repository-admin permission on `TauCetiProject/TauCeti` with the same predicate as the security audit
(`tauceti_worker/security_audit.py::require_admin`). A change applies to new scheduling decisions,
is persisted in the store, and appears in `status.settings` immediately. Values are positive integers;
anything else is rejected before the request is sent. `--url` points the command at the public endpoint
when run from another machine.

## Cutover

Preconditions (plan, "Transition to production"): two days of shadow agreement with Bors and the
rehearsal on `TauCetiProject/TauCetiQueueScratch` passed. The cutover itself is one setting change
with two halves, made together, nothing drained:

1. Take a backup (`backup.sh`).
2. Leave the TauCeti repository variable `MERGE_BACKEND` at `bors`. The service is a drop-in for
   Bors's commands, dispatch, statuses and feeds, and every workflow caller only distinguishes
   `bors` from anything else (`docs/queue/github.md`, "MERGE_BACKEND"): `pr-build.yml` and
   `retry-ci-status.yml` verify scope approvals against the observation feed under `bors`,
   `queue-release.yml` is skipped under `bors`, and TauCetiReview's `merge-only.yml` and
   `merge-sweep.yml` publish `merge eligibility` checks and leave admission to the selected backend
   under `bors`. The cutover is therefore not a variable change: point the `bors.taucetiproject.org`
   hostname (the feeds the workflows read) and the TauCeti webhook deliveries at the service by
   suspending the `tau-ceti-bors` App installation, and enable the service's publication. Bors
   receives no further deliveries from then on; the service's own approval intake is authoritative.
3. Enable publication: set `publication_enabled = true` in `config.toml` and run
   `systemctl --user restart tauceti-queue`, then confirm `service.mode` is `production` in status.
   (A restart is needed because this flag is process configuration, not a runtime setting; it is the
   deliberate friction of the plan's "one setting change".)
4. The service imports Bors's current state from the observation feed on the transition to
   production: every approved PR waiting or in an unsealed batch becomes an approved input with its
   original admission age; Bors's running head is left to finish and land under Bors, and the service
   seals its first head only after observing that landing as a canonical advance. Bors's remaining
   sealed batches are released back to approved inputs with their ages. Status shows the imported
   inputs under `approved_waiting` and the first head when it seals.
5. Watch the first three heads as the plan's "First week" says, and run the journal report daily.

Rollback is the same two halves in reverse: restore the `tau-ceti-bors` installation and hostname and
set `publication_enabled = false` (restart). The service returns to shadow mode with its records intact; Bors
resumes approval intake. Approved inputs the service holds at that moment are re-approved under Bors by
their authors or the review App; the service's observation feed lists them.

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
inputs waiting (`approved_waiting`), the pending task (`pending_task`), the service's running revision
and start time (`service.revision`, `service.started_at`), and the effective settings (`settings`).
"Which machine is this running on and is it moving" is answered by `head.ci.runner`, `head.ci.host` and
the newest `updated_at` among the phases; `tauceti-queue status` prints exactly these.

The journal report (`scripts/staging_journal_report.py`) runs daily and after every change against the
service's candidates feed and the Bors baseline window ([journal-report.md](journal-report.md)): landed
PRs per hour, head cycle time and its CI/repair split, repair rounds per landed head, repair time and
inference cost per task, shrink events, singleton builds (expected zero), claim collisions, PR waiting
age, inference holds and stall conditions, each beside its baseline value.

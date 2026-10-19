# Prepared activity reports

`tauceti activity` reads a prepared summary. It does not query GitHub or the shared
state server, open SQLite, scan logs, start a collector, or refresh stale data.
`tauceti workers status` includes the same installation-wide summary.

```sh
tauceti activity
tauceti activity --worker reviewer --json
tauceti activity --group FLEET_MACHINE_ID
```

Finished rounds use their finish receipt in trailing 3-hour, 24-hour, and 7-day
windows and a recorded-total column. The type is the final selected work unit,
such as `roadmap`, `review`, `fix`, or `security-audit`; fallback candidates are
not additional rounds. All supported round types appear even when their counts
are zero; the prepared summary publishes the worker taxonomy for other consumers.
No-work surveys have their own row. Admission/GitHub
waiting attempts and dry runs are excluded. Failed/interrupted rounds are
included, with a failure subset (including timeouts). Running rounds and
processes interrupted without a finish receipt are separate. Discovery of an
interruption never supplies a fabricated finish time.

PR authorship comes from successful tracked creation, not the entire GitHub
account's history. Authored counts use creation time; landed counts use landing
time, including PRs created before the window. Open PRs are the current last-known
stock. Unknown states, landing evidence, and historical outcomes are explicit.
Closed never means landed. Bors landings require an exact PR-head match in
verified batch-to-main evidence, not titles, reviews or staging success.

## Preparation and freshness

Workers write small private receipts into `~/.local/state/tauceti/activity/pending`.
The existing manager prepares reports in a separate thread every minute, outside
its supervision loop. Receipts are consumed idempotently into indexed SQLite.
A nonblocking preparation lock prevents duplicate collectors. Atomic publication
retains last-known data after a failure and exposes that failure and data age.
Summaries older than three minutes are labelled stale. Windows end at each
summary's preparation time.

Each worker and fleet group has its own small summary file, so status does not
load the entire historical inventory. A separate prepared PR identity inventory
supports deduplicated aggregation: `tauceti activity --inventory` (optionally
scoped by worker or group).

```sh
# Explicit preparation, using configured shared-server credentials; never GitHub.
tauceti activity refresh --config ~/.config/tauceti/workers.toml
# Import PR creation receipts since activity recording began; follow by offset.
tauceti activity refresh --backfill
```

Activity starts with native round lifecycle recording. Preparation discards
previously imported resource-log rounds and PR activity before that starting
point. Resource logs are never imported, and the cutoff is retained across
journal replays and upgrades. Native rounds already recorded are preserved.

Without a running manager, Linux installations can optionally enable preparation:

```sh
tauceti activity install-timer --config ~/.config/tauceti/workers.toml
# To stop recurring preparation:
systemctl --user disable --now tauceti-activity.timer
```

This installs a user timer with one-minute spacing and a lower-priority oneshot
service. It needs a working user service manager, not root. It is unnecessary
when the manager already prepares this cache. `TAUCETI_ACTIVITY_DIR` overrides the
activity directory and is exported before worker HOME isolation.

## No additional GitHub API requests

The preparer requests up to 500 tracked PR headers per call to the shared server's
authenticated `POST /v1/activity`, at most once per five minutes per unlanded PR.
It uses worker configuration's `github_state_url` and `github_state_token_file`,
or the corresponding `TAUCETI_GITHUB_STATE_*` environment variables.

This route never registers demand, configures repository ingestion, schedules
repairs, reactivates closed-PR polling, or falls back to the GitHub facade.
Redirects are refused. Failed/missing observations retain last-known facts and
are not retried before the next interval. The endpoint returns stored states,
original observation timestamps and explicit missing numbers. Existing webhook
and reconciliation behavior is unchanged.

Existing webhook `merged_at` data supplies ordinary GitHub merge evidence.
Already-present TauCetiCI SQLite data supplies Bors landing times; set
`TAUCETI_ACTIVITY_BORS_DB` to its path (the default checks the existing fleet
cache). Reporting never downloads or refreshes that database. Without evidence,
landing counts remain incomplete and the unknown count is displayed.

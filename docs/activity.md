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
Closed never means landed. Bors squash-merges can leave GitHub's state as closed;
the shared server supplies the recorded promotion to main, bound to the exact
approved PR head and constructed batch commit. Titles, reviews, CI success and
staging pushes are not landing evidence. Ordinary closed PRs do not produce a
missing-landing warning.

## Costs and initiated PR output

Average API spending has **3h**, **24h**, **7d** and **All time** columns in
$/hour. It uses completed/reconciled session costs, including reviews, recognized
at settlement; active reservations are separate estimates and do not enter this
rate. Each average divides by elapsed recorded time within that window, so a
new installation is not diluted by hours before recording began. `Worker-hours
recorded` adds time across completed and currently running native rounds, clipped
to each window. Capacity/budget/quota waiting and interruptions without a known
end do not inflate it. Failed rounds with recorded durations still count. The
spending rate remains dollars per elapsed hour, not dollars per worker-hour.
Fleet averages pool spending over the longest recorded
duration covered by their host summaries; parallel machines do not add elapsed
wall-clock hours. Missing spending data leaves the fleet rate unavailable.
Preparation reuses the existing local budget cache and collector, with no extra
tasks or GitHub requests.

Cost estimates have **All time** and **7 days** columns. They pool every
conversation in the reported worker/machine scope, including reviews and fixes
on PRs initiated elsewhere. Completed sessions use their reconciled budget
receipts at the original settlement time; ongoing sessions use the budget's
current reservations, shown separately as active estimates. These are API costs,
not machine charges or subscription fees. Reading the ledger never changes it.

The seven-day denominator contains only PRs initiated within those seven days;
all-time contains all retained initiated PRs. Each verified landed PR contributes
its Lean lines added, each currently open PR contributes half its Lean lines
added, and a closed unmerged PR contributes nothing. Reviews and other assists
never add another author's LOC to this denominator. Fleet totals pool costs and
weighted LOC before dividing; they do not average machine ratios.

LOC comes from complete file lists already cached by the shared GitHub state
server. Immutable file URLs must match the PR head (or base for deleted files),
and cached file totals must agree with available PR headers. The observation
route never schedules file collection or falls back to GitHub. Missing or stale
LOC is extrapolated from measured PRs in the same window: measured weighted LOC
per measured PR weight, multiplied by total eligible PR weight (landed = 1,
open = ½). Unknown PR states contribute zero. These estimates carry `~` and show
measured-PR coverage. With no measured LOC, missing LOC contributes zero; positive
spending divided by zero LOC displays `$∞` (zero spending displays `$0.0000`).
Missing financial data remains explicitly unavailable. Fleet totals pool the
measurement counts and costs before estimating and dividing.
Cost preparation runs in the existing background collector;
`activity` and `workers status` still only read small JSON summaries.

Completed review counts include reviews of other authors' PRs. `PRs assisted`
counts distinct PRs in the reported scope with completed review/fix/rebase work
or a branch push, excluding that scope's initiated PRs. Multi-host summaries add
review events, while unique assisted-PR counts remain per-host.

Existing local CI receipts are imported read-only and incrementally when a fleet
controller is present; this adds no GitHub requests. Non-fleet consumers can set
`TAUCETI_ACTIVITY_CI_DB` to a compatible receipt database or omit this optional
source.

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

The preparer requests up to 100 tracked PR headers and cached LOC counts per call to the shared server's
authenticated `POST /v1/activity`, at most once per five minutes per unlanded PR.
It uses worker configuration's `github_state_url` and `github_state_token_file`,
or the corresponding `TAUCETI_GITHUB_STATE_*` environment variables.

This route never registers demand, configures repository ingestion, schedules
repairs, reactivates closed-PR polling, or falls back to the GitHub facade.
Redirects are refused. Failed/missing observations retain last-known facts and
are not retried before the next interval. The endpoint returns stored states,
original observation timestamps and explicit missing numbers. Existing webhook
and reconciliation behavior is unchanged.

Existing webhook `merged_at` data supplies ordinary GitHub merge evidence. The
shared server's existing cron also imports Bors's stored candidate/promotion
journal at most once per five minutes for the whole fleet. This reads Bors's
database, not GitHub, and neither triggers merges nor changes queue state. The
activity endpoint only reads the resulting cache. Promotion receipts require
matching construction and main-push (or recovery-read) evidence, preserving the
original recorded timestamp and exact approved PR head across squash merges.

Already-present TauCetiCI SQLite data remains an optional Bors landing source; set
`TAUCETI_ACTIVITY_BORS_DB` to its path (the default checks the existing fleet
cache). Reporting never downloads or refreshes that database.

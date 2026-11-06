# Public queue monitoring

The standalone `tauceti_queue.monitoring` sidecar reads SQLite in read-only mode,
projects explicit public fields and publishes framed observations to the existing
live-CI service. It cannot claim tasks, write statuses, pause admissions or land a
batch. Its failure does not affect controller or repair work.

Deploy the live-CI reader, public scope and telemetry-only grant first. Keep the
publisher token and state file private. Start a separate supervised process:

```sh
python3 -m tauceti_queue.monitoring \
  --database ~/TauCetiQueue-data/queue.sqlite3 \
  --clone ~/TauCetiQueue-data/clone \
  --worker-root ~/TauCetiQueue-data/repair \
  --worker-root ~/TauCetiQueue-data/repair-2 \
  --output ~/TauCetiQueue-data/monitoring/queue.json \
  --state-file ~/TauCetiQueue-data/monitoring/publisher.json \
  --publish-url https://ci-data.taucetiproject.org/ingest/operations/queue \
  --token-file ~/TauCetiQueue-data/monitoring/token \
  --follow --interval 10
```

Run it from an installed release or source checkout. The module is stdlib-only.
The controller/repair services do not need to restart to start the sidecar. Live
source updates that add the reduction journal event use the normal live deployment
path without draining. Do not run the controller installer solely to add telemetry.

The state file persists a source UUID and monotonic restart epoch and takes a
single-process lock. Never run two publishers for the same store. Uploads commit
only after all bounded frames validate; acknowledgements advance the delta cache.
A lost receiver base requests a full resend. Source-complete deletions use explicit
tombstones. Capped missing history does not imply deletion. Keep state files in
backups. If a state file is lost, stop its old sidecar and use `--recover-state`
with a **new** private state-file path, the same endpoint and queue grant. This
explicitly adopts the established receiver source/fence; do not use it to connect
an unrelated queue/store. Restart epochs also advance from current milliseconds,
so an ordinary restored old state file does not replay old process epochs.

The live detail window retains active batches plus up to 500 terminal batches in
ninety days, and up to 5,000 terminal inputs. Byte caps can shorten old detail;
extraordinary active-only overflow is marked partial. Authoritative batch counts
remain distinct from retained member detail. The public report records its source
scope and observation times. Worker checkpoint/outcome summaries contain only
phase/observation/duration, approved provider/model labels and recorded cost
coverage. Recorded token subtotals retain known-attempt and missing-attempt counts
for each token category; absent usage is not zero. The canonical tip comes from
the existing controller status fact, without another Git scan. No raw task payload, claim token, worker path, prompt, model response,
process command, environment or private error is exported.

Repair workers write an optional atomic `progress.json` beside each private checkpoint. It contains
the sanitized summary and a local lease binding; the exporter omits that binding. When this file is
present, monitoring never opens the task or outcome payload, so member context, prompts and final
diagnostics can grow without making progress unavailable. Accounting includes every recorded attempt
once; only the model-name display is sampled (with `models_truncated`), independently of cost and
token totals. A failed progress write leaves the last observation in place and cannot fail a repair.
Older running workers and historical tasks remain readable without an immediate worker restart.
That compatibility path loads trusted local checkpoints/outcomes with memory proportional to their
size; it has no task-size cutoff. Public frame and transport bounds remain unchanged.
Open tasks may also carry `progress.build`: preparation, verification or streaming
stage, Lake's reported completed/total **build tasks**, sampler time, log update
time and the last observed increase in completed tasks. These are not module
counts or a CI verdict. The sidecar reads at most the last 64 KB of a known local
build log; no log text, module names or paths leave the machine. The first sample
has unknown progress-change time. Unchanged counts, including a heartbeat or
other new log output, do not advance that time. Log replacement, truncation,
counter regression, a new phase or claim resets the comparison. The phase's own
checkpoint time stays unchanged. A silent log alone is not a stalled build.

Worker liveness is distinct from task progress. Busy workers may not rewrite
worker.json until a long task finishes. On Linux the sidecar can instead verify
the recorded process using its owner, module, root and birth time (fencing reused
PIDs). Its observation time then records actual local liveness; a current claim
and recorded task phase supply the displayed activity. An old checkpoint without
that evidence stays dated. Missing/inaccessible process evidence means unknown,
not dead or idle. No PID, command arguments or private paths are exported, and
the observer never signals a process or controls the task's execution time.

A separate hourly report reuses the existing pilot calculations:

```sh
python3 -m tauceti_queue.monitoring_history \
  --database ~/TauCetiQueue-data/queue.sqlite3 \
  --clone ~/TauCetiQueue-data/clone \
  --worker-root ~/TauCetiQueue-data/repair \
  --worker-root ~/TauCetiQueue-data/repair-2 \
  --baseline ~/TauCetiQueue-data/reports/bors-baseline-2026-11-03.json \
  --output ~/TauCetiQueue-data/monitoring/history.json \
  --publish-url https://ci-data.taucetiproject.org/ingest/operations/history \
  --token-file ~/TauCetiQueue-data/monitoring/token
```

Schedule this independently of the live publisher. It computes 24-hour, seven-day
and whole-pilot windows using wall-clock denominators including downtime, unique
PR identities, retained nonempty commits and unions of CI/claimed-task intervals.
Overlap is reported separately. Recorded inference costs are subtotals; absent
estimates and interrupted sessions remain unknown. Baseline nonempty contributions
and inference costs are unavailable. Attribution/comment correctness and the
warm-artifact condition still require a separate audit. The public projection
contains bounded numeric summaries and local IDs, never the private snapshot,
outcome paths or raw journal detail.

A final empty commit alone does not establish why content disappeared. New
accepted adoptions journal explicit `member-reduced-to-empty` facts; retained
scripted already-landed notes remain separate. Historical ambiguous empties stay
`empty_unknown`.

The module's local JSON output is prepared public data; keep its parent directory
private until it has been inspected against the configured scope. Public browsers
read R2 only, never this file or the queue's database. Queue publication has a
15 MB framed-snapshot budget and a 12 MB live projection target; oversize records
fail before network calls. Normal batches do not change queue scheduling to fit
monitoring limits. Explicit retained-batch scope lets the receiver retire old
terminal details without guessing that capped absence means a deleted live item.

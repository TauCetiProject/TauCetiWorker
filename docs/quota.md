# Quota and pacing

The README covers the pacing rule and the controls. This page covers where the
usage numbers come from and how the pacer behaves at the edges.

## Credential sources

The pacer reads the credential files the official CLIs already maintain,
`~/.claude/.credentials.json` and `~/.codex/auth.json`, and queries each
provider's usage endpoint. It honors `$CLAUDE_CONFIG_DIR`, so switching between
a personal and a work Claude account is paced correctly.

Reading quota never sends a model request. `tauceti status`, the dashboard, and an auto selection
that lands on Codex make no model request at all.

## Sharing Claude usage and backing off

Workers using the same Claude credential source share usage observations, a fetch
lock, and rate-limit cooldowns under `<claude-config>/.tauceti-quota/usage/`.
Isolated workers follow their credential-source marker back to that same location,
including across projects and checkouts. This is built into Tau Ceti and requires
no external usage script, service, or account setup. Different credential-source
directories remain independent; this does not coordinate across machines.
If the credential directory is read-only, as in the Docker deployment, the shared
worker-state root holds these records instead. This fallback is shared within the
project's state root, rather than across separate project volumes. Credentials
need no write access.

A loop refresh reuses headroom measured less than 20 minutes ago. A pace-blocked
reading is refreshed when its expected recovery time arrives. The lock allows
only one worker to fetch; other workers use its result, or return a recent reading
immediately while a sibling refreshes it. Once an old refusal reaches its recovery
time, callers wait for that shared refresh instead of returning the old refusal.
During a transient network failure or usage-endpoint HTTP 429, a still-valid
reading can supply headroom for up to one hour. Samples never survive a quota
window reset or cross to a different credential. Pacing uses the sample's original
observation time, so an old over-budget reading cannot become permission to run
merely because time passed. Actual provider limits still apply to model sessions.

An HTTP 429 throttles the usage endpoint; it does not by itself mean the model
quota is exhausted. All workers sharing that source honor its finite `Retry-After`
up to 24 hours for the same credential or a verified account identity. Invalid or larger values are logged
and ignored so corrupt data cannot park unattended workers indefinitely.
Without that header, transient HTTP and connection failures use shared
exponential backoff with jitter up to fifteen minutes. A successful usage read
clears that backoff. The loop wakes at a cooldown's deadline, rounded up to the
next second, rather than adding another full poll interval. It revisits long
waits hourly without making an early HTTP request.

When the only account identity is a credential-source path, a changed token may
represent a different account. Its inherited cooldown is limited to fifteen
minutes from the original refusal, allowing fresh discovery without a token-change
request storm. The original credential still honors its complete server deadline.

Without a usable reading, normally paced workers wait for telemetry. An explicit
`--agent claude --ignore-quota` or `--agent codex --ignore-quota` may attempt useful
work through a transient telemetry outage: HTTP 408, 429, 500, 502, 503, 504, 529,
or a connection failure. This does not invent a quota value or make an expired
positive sample usable. Known exhausted windows remain blocking until they reset,
even after the sample's ordinary TTL, scoped to the credential that supplied them.
A changed credential may represent a different account and cannot inherit another
credential's quota observation. HTTP 401 credential rejections remain blocking for
that token until a successful usage response; a new token has its own identity.
An HTTP 403 does not establish a durable authentication verdict without evidence
distinguishing an edge challenge. A still-valid cached reading can supply headroom;
otherwise it blocks launches. Its `Retry-After` is honored for that credential,
without preventing a newly selected credential from recovering.
Missing credentials, malformed responses, and other HTTP errors still block.
Actual model refusals stop the round and use the loop's existing escalating
backoff. A subscription-limit refusal from an authoring agent also records a
bounded fifteen-minute retry against its frozen launch credential. It expires
automatically, clears on a successful usage read, and cannot block a newly selected
credential. A subscription refusal never consumes a PR's infrastructure-refund
allowance. A review-engine provider refusal posts nothing and does not charge the
PR's review-error budget.

Loop-launched operational audit recovery retains its exemption from soft pacing,
including when its Codex recovery provider differs from the audit provider.
This allows retained audit work to repair itself without an additional burn-curve
wait. Known hard blocks and normally paced telemetry requirements still apply.

Actual usage HTTP attempts are recorded separately from cached status reads under
`<worker-state-root>/quota-requests/requests.jsonl`. Each attempt has a `request`
record and, if it completes, a matching `response` record. Fields include worker,
provider, timestamps, duration, HTTP status, raw and parsed `Retry-After`, server
request ID, and cache age. No request headers, response bodies, or tokens are
recorded. One active file and two backups retain about 6 MiB. Recording is best
effort: a busy lock or unavailable disk never prevents a request or launch.
These are TauCeti's records only; they do not count other clients' requests or
integrate with external usage scripts or services.

## Local checks during quota waits

Subscription loops check local credential and TauCeti usage records every 25
seconds during a quota wait. They can notice a changed file-backed credential or
a new shared Claude observation without waiting out an old, hour-long sleep.
An early network attempt can be staggered by up to five seconds; cached recovery
has no additional delay. These local checks make no HTTP requests, run no
subprocesses, acquire no fetch locks, and do not renew or mirror credentials.
They use existing TauCeti-owned records, including the read-only-mount fallback;
they do not coordinate with personal usage tools.

An early wake returns through normal provider selection and launch checks.
Each provider retains its own scheduled refresh deadline: a Claude change does
not refresh an unchanged Codex provider, even when Codex's cache has expired.
Repeated local updates cannot postpone the original normal recheck. A newly
applicable earlier cooldown expiry is also checked without needing another file
change. Actual HTTP retries still honor the current identity's persisted
cooldowns. Extra early attempts are bounded within the wait; ordinary due
refreshes and authorized launches remain unaffected.

Observation errors retain the normal scheduled retry. Isolated workers observe
their canonical credential source and use the existing mirror synchronization
when selection resumes. Keychain-only changes on macOS are discovered at the
normal credential lookup, avoiding additional Keychain prompts or subprocess
polling. Custom `--quota-cmd` commands retain their existing schedule.

Runtime status keeps `next_action_at` as the normal quota recheck, with the local
check cadence in `quota_local_check_seconds` and that recheck deadline in
`quota_refresh_at`. Local checks do not write status or log a heartbeat on every
tick. GitHub quota waits and no-progress backoff retain their separate schedules.

## Keeping the Claude token alive: `--auto-refresh`

By default the worker never rotates a refresh token. When the Claude access token
expires it reports Claude unavailable, and your next `claude` run renews it. That
is fine at a keyboard and fatal unattended: `work --loop` will sit at
`claude usage HTTP 401 (access token expired or rejected; log in again)` until
someone intervenes.

`tauceti work --loop --auto-refresh` (or `$TAUCETI_AUTO_REFRESH=1`) lets the
worker renew the token itself once it is within 90 minutes of expiry.

**Only turn it on when nothing else uses that credential file.** Claude and Codex
issue single-use refresh tokens: exchanging one retires it and returns a
replacement. TauCeti serializes its own processes on the host, but it cannot
serialize an interactive `claude` sharing `~/.claude/.credentials.json`, a second
refresher, or a copy of the credential on another machine — a rotation here logs
any of those out. The shape this is meant for is a worker running as its own
user, with its own Claude account nobody signs into interactively;
`$CLAUDE_CONFIG_DIR` gives the same separation on a shared login. On macOS the
flag does nothing: the Keychain is the store, and the section below applies
instead.

When it is on: it renews the file the operator owns, never a worker's stripped
mirror, and it never touches a credential carrying no refresh token, so the
Docker deployment's dedicated refresher stays the single writer there. Rotations
are rate-limited by markers beside the credential, shared across every worker on
the host. Only the paths about to run something renew — the loop pacing towards a
round, a round resolving the model it will launch, and the launch stage. Reading
commands stay reads: `tauceti status` and the dashboard report an expired token
rather than rotating it behind you.

## macOS and the login Keychain

On macOS, Claude Code keeps its credentials in the login Keychain rather than in
a file. The pacer reads them from the Keychain, read-only. It never refreshes
the Keychain, because that would log out your interactive `claude`. So on token
expiry it simply reports Claude unavailable for the cycle, and your next `claude`
run refreshes the Keychain so the pacer can read it again. `--auto-refresh` does
nothing here, so that run is the only way back.

A locked Keychain, which is what you get headless or over SSH, reports
unavailable with a hint to `security unlock-keychain` first.

Bubble uses the credential file directly where Claude stores credentials in a
file; on macOS it receives the Keychain credential through the private handoff
described in [the sandbox notes](sandbox.md).

## Reading Claude's two windows

Claude's session and weekly windows reset on separate clocks, so they are read
independently and neither is inferred from the other. Each window's raw state is
kept before any pacing is applied.

The structured `limits` array is authoritative for each window. Legacy flat keys
are used only when `limits` omits that window; they cannot override a structured
entry.

A missing window or invalid data, such as an unreadable reset timestamp or
non-numeric usage, stops the provider and reports the specific problem:

```
weekly limit missing from usage response
session reset timestamp invalid
```

rather than a generic "usage unknown". An unreadable constraint is not the same
as no constraint.

## Idle quota windows

A recognized idle window, such as `five_hour: null`, permits ordinary useful work
when the other limits permit it. No dummy initialization turn, reservation ledger,
or wait for active usage telemetry is needed. Status says `no active window
reported`; it does not invent usage or a reset clock. Missing and malformed
constraints still block. An idle weekly record has no measured weekly pace.

The existing startup-spend policy remains: a deliberately zero-spend opening
interval, such as `--pace 0:0,90:0,100:95`, blocks idle first use. The default curve
permits it. Active sibling windows retain normal pacing and explicit overrides.

Valid active/idle observations share the existing cache and twenty-minute refresh
cadence, with the one-hour TTL and earliest original active reset as expiry bounds.
An old active observation expires at its reset even if a newer read would call
that clock idle. Cached observations retain their original pacing verdict; time
alone cannot turn an old over-pace reading into permission. A cached idle sample
may permit several workers to run before the next normal refresh, as cached active
headroom can lag actual spend. There is no one-request allowance or account permit.

Actual model-limit refusals use the existing bounded retry (up to fifteen minutes).
A usage HTTP 200, idle or active, cannot clear that refusal; its retry expires
automatically without requiring positive telemetry. Credential rejection and
recovery remain separate. Old bootstrap records are ignored and need no cleanup.
Observation commands and rounds with no eligible work make no model requests.

## Why "strictly under"

A provider is available while `used%` is strictly under the budget for the
elapsed fraction of the window. Strictly, because the request being decided
costs something: sitting exactly on the budget
(`session at budget (20% elapsed: used 50% = 50% pace budget)`) is a pause, not a green light.

If usage cannot be read at all, the provider is treated as unavailable rather
than assumed free.

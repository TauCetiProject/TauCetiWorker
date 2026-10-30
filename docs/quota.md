# Quota and pacing

The README covers the pacing rule and the controls. This page covers where the
usage numbers come from and how the pacer behaves at the edges.

## Credential sources

The pacer reads the credential files the official CLIs already maintain,
`~/.claude/.credentials.json` and `~/.codex/auth.json`, and queries each
provider's usage endpoint. It honors `$CLAUDE_CONFIG_DIR`, so switching between
a personal and a work Claude account is paced correctly.

Reading quota never spends anything, with the one exception described under
"window bootstrap" below. `tauceti status`, the dashboard, and an auto selection
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
An HTTP 403 blocks its current attempt but does not establish a durable
authentication verdict without evidence distinguishing an edge challenge.
Missing credentials, malformed responses, and other HTTP errors still block.
Actual model refusals stop the round and use the loop's existing escalating
backoff. A subscription-limit refusal from an authoring agent also records a
bounded fifteen-minute retry against its frozen launch credential. It expires
automatically, clears on a successful usage read, and cannot block a newly selected
credential. A subscription refusal never consumes a PR's infrastructure-refund
allowance. A review-engine provider refusal posts nothing and does not charge the
PR's review-error budget.

Actual usage HTTP attempts are recorded separately from cached status reads under
`<worker-state-root>/quota-requests/requests.jsonl`. Each attempt has a `request`
record and, if it completes, a matching `response` record. Fields include worker,
provider, timestamps, duration, HTTP status, raw and parsed `Retry-After`, server
request ID, and cache age. No request headers, response bodies, or tokens are
recorded. One active file and two backups retain about 6 MiB. Recording is best
effort: a busy lock or unavailable disk never prevents a request or launch.
These are TauCeti's records only; they do not count other clients' requests or
integrate with external usage scripts or services.

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

## The window bootstrap

There is one gap where the endpoint reports a window with no usage and no reset
clock: right after that window rolls. Only a Claude request can open the new
window, so `tauceti` makes one small `claude -p` turn to do it, drops the cached
usage, and re-reads. The fresh telemetry, not the request, then decides whether a
round runs.

The bootstrap runs only under these conditions:

- It happens at the launch stage of a round that has already found work, so a
  poll that finds nothing to run costs nothing.
- Every other window must be active with real headroom. A window that is at
  budget, over pace, exhausted, missing, or unreadable forbids the bootstrap.
- It respects your pace curve. Under a curve whose budget stays at 0 for the
  first stretch of a window, say `--pace 0:0,90:0,100:95`, a fresh window may not
  be opened at all, and the status says so (`pace budget stays 0% through 90% of
  the window`) rather than quietly opening one to manufacture a clock.
- It is claimed under a lock in a shared ledger beside your credentials *before*
  the request goes out, so every worker on that account, whatever its worker id,
  checkout, or isolated `$HOME`, makes at most one request per window period,
  even if one of them crashes mid-flight.

If the window still is not reporting afterwards, the status reads
`session bootstrap attempted; awaiting fresh usage` and the worker stays parked.

## Why "strictly under"

A provider is available while `used%` is strictly under the budget for the
elapsed fraction of the window. Strictly, because the request being decided
costs something: sitting exactly on the budget
(`session at budget (20% elapsed: used 50% = 50% pace budget)`) is a pause, not a green light.

If usage cannot be read at all, the provider is treated as unavailable rather
than assumed free.

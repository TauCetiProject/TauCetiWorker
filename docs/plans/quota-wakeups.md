# Responsive quota waits

Implementation: `tauceti_worker/quota_wait.py`, integrated with subscription provider selection in `loop.py` and `quota.py`. The verification and rollout requirements below are release criteria.

## Problem and intended outcome

Workers should notice usable capacity within 30 seconds of a file-backed credential change or a new, valid TauCeti usage observation for the current credential. A provider's HTTP retry deadline must continue to govern requests to its usage endpoint independently of how often a sleeping worker checks local state. Platform credential stores and custom quota commands retain their normal lookup schedule; their limitations are specified below.

The fleet investigation on 2026-10-30 found this sequence:

- At 06:46 UTC, Claude's usage endpoint returned HTTP 429 with `Retry-After: 3600`.
- At 07:01, worker5 obtained a successful usage response under a changed credential and continued working. Later cached observations showed both Claude windows under pace.
- Workers 4, 6, and 7 retained uninterrupted sleeps until 07:46. They could not notice the changed credential or available shared observation during those waits.
- On waking, those workers cleared the Claude check and encountered exhausted GitHub REST quota instead. That separate limit means early quota recovery does not guarantee an immediate model launch.

The scheduling defect is the uninterrupted `time.sleep(nap)` in the subscription quota-wait branches of `tauceti_worker/loop.py`. Simply shortening every sleep and re-running `choose_model(..., refresh=True, renew=True)` would increase usage HTTP requests and credential-refresh attempts. Local checks need an explicitly side-effect-free path.

Evidence: `/home/kim/.local/state/tauceti/fleet-investigations/20261030-quota-sleep/report.json`.

## Required behaviour

1. Local observation checks complete at most 30 seconds apart during subscription quota waits, or sooner when an existing eligibility/reset/retry deadline arrives. A tick itself makes no network request, runs no subprocess or external quota command, renews no credential, and spends no model tokens. Include bounded observation time in the cadence rather than adding it after a thirty-second sleep.
2. An unchanged local state keeps the existing provider refresh schedule. Thirty-second checks must not become thirty-second HTTP polling, including for Codex when Claude is the provider whose state changed.
3. A changed credential is evaluated using existing identity and cooldown rules. A token change does not prove an account change and must not clear a known account cooldown. Preserve the existing bounded handling when only a credential-source identity is available.
4. A newly observed cache entry can supply headroom only if the existing validators accept its identity, observation time, TTL, reset clocks, payload, and pacing policy. Time passing cannot turn an old over-pace reading into launch permission.
5. Preserve authentication refusals, known exhaustion until reset, model refusals, bootstrap reservations, forced-provider choices, and the existing `--ignore-quota` semantics. Local observation never broadens permission to launch.
6. Failed or unavailable local observation falls back to the existing scheduled recheck. A missing file, permissions error, corrupt cache, unavailable platform credential store, or failed diagnostic write must not add a new permanent wait. Compare successful observations only: a failed read followed by recovery to the same value is not a credential change.
7. Keep the behaviour automatic. No new service, required configuration, operator-maintained marker, or filesystem notification facility is needed.
8. Read credentials only through the existing supported credential accessors. Existing `<credential source>/.tauceti-quota/` records and their worker-state fallback are TauCeti-owned and remain in scope. Do not integrate with personal `~/.claude` usage scripts, logs, caches, or coordination mechanisms, and do not add files, locks, or watchers there.
9. Capture absolute provider recheck deadlines once. Local changes may advance those deadlines, but must never postpone the original normal recheck or its existing hourly revisit bound. Time-based release is itself a reason to re-evaluate, even when no file changes.

## Implementation sequence

### 1. Establish the current baseline

Use the current upstream default branch, retaining PR #420 behaviour: plausible HTTP `Retry-After` values are honored in full, and explicit `--ignore-quota` can continue through eligible telemetry outages. Older fifteen-minute caps on explicit server retry headers are not the intended baseline.

Inspect both subscription quota-wait branches, provider selection, credential lookup, cache validation, cooldown scoping, and launch-stage checks. Preserve their current policies rather than reproducing them in a second implementation.

### 2. Add an explicit local observation interface

Add a small interface in `tauceti_worker/quota.py` that reads local provider state without falling back to HTTP or renewing credentials. Do not assume `refresh=False` guarantees this: existing provider methods can fetch on a cache miss.

Return enough information to identify meaningful changes and reuse the normal branch-specific selection logic:

- Current credential identity and supported credential-source identity, held privately in memory.
- The relevant usage observation identity and original observation timestamp.
- Applicable cooldown, exhaustion, authentication-refusal, and model-refusal state.
- A provider verdict derived through the existing validators, or an explicit local-unavailable result.

Inspect Claude's existing primary and worker-state fallback locations without attempting a write or taking the fetch lock. Today the fallback is selected only after a write fails on a particular `Quota` instance; a fresh read-only observer cannot rely on that sticky instance attribute. Both locations supply change hints, not a merged authorization verdict. Keep ordinary lock-driven store selection and refusal semantics unchanged; normal selection after a wake validates the effective store, credential/account scope, and applicable in-process restrictions before deciding what can run. Carry the effective store from an ordinary selection into any cache-only reads that need it; an unknown store is local-unavailable, not permission to fetch or launch. In particular, do not union stale refusals from abandoned locations into a new permanent block: existing auth-refusal records can lack timestamps, so observation time cannot establish their supersession. Test conflicting records as well as a read-only credential mount.

For isolated homes, extend the supported accessors to inspect the canonical source named by the existing credential-source marker, read-only. The isolated mirror alone cannot reveal an external credential change. A source change is a wake hint; normal provider selection must run the existing `mirror_creds` synchronization before measuring or launching with that credential. Local ticks never rewrite a mirror or credential file. A synchronization failure follows the existing provider policy and must never authorize one identity using another's observation.

Use a semantic observation signature: identities, validated observation contents and original timestamps, absolute cooldown/refusal deadlines, and relevant bootstrap episode state. Exclude elapsed cache age, countdown values, filesystem modification time alone, and unrelated metadata. Detect atomic replacement with the same timestamp and do not repeatedly wake on identical invalid content. Keep the last successful signature across transient read failures.

Read bounded files; do not scan directories or histories. On macOS, local ticks check existing cache/cooldown/refusal records for the last established identity without invoking `security`. Keychain credential changes are discovered at the existing normal recheck, with existing timeouts and warning behaviour. This avoids adding repeated Keychain prompts, thirty-second subprocess stalls, or warning spam; the thirty-second file-credential recovery promise does not apply to Keychain-only changes. Pass a captured credential/account snapshot through validators so idle/bootstrap annotation cannot perform hidden credential lookups.

Wrap the local observation boundary in `except Exception` and treat failures as local-unavailable, retaining the scheduled recheck and applicable known restrictions. Do not catch `BaseException`: stop signals and `KeyboardInterrupt` must propagate. Keep observer reads free of mandatory shared locks. Reconstruct request-scoped provider state or explicitly resolve it each time; do not retain stale `Quota` instance directory/credential memoization across ticks.

Do not expose tokens, credential contents, or raw usage payloads in status or logs.

### 3. Replace uninterrupted subscription quota sleeps

Introduce one wait helper used by the normal paced and explicit `--ignore-quota` branches. Keep the existing provider deadline calculation for when normal provider refresh becomes due. Give the helper an injected wall clock, monotonic clock, and sleeper so tests advance time consistently.

The helper should:

1. Capture a successful observer signature immediately before ordinary provider selection and compare it with an observer reading on wait entry. Compare observer output with observer output, rather than comparing the observer's canonical source/both-store view with selection's mirror/effective-store view. This catches changes during selection without treating a persistent source/mirror or primary/fallback disagreement as a new change on every entry. Retain the last successfully handled signature across wake-triggered selection and subsequent blocked iterations; a failed baseline read remains local-unavailable and falls back to scheduled checks.
2. Sleep until the earlier of the next local tick or that deadline, using monotonic time for elapsed waits. Re-evaluate wall-clock reset and retry timestamps through the existing policy when checking state.
3. On a local tick, read the local observation. If it is unchanged and no relevant absolute deadline has arrived, continue waiting without calling normal provider selection.
4. On a meaningful change or deadline, return a wake reason and per-provider refresh permissions to the outer loop. Run existing admission, recovery, branch-specific selection, and launch checks. The helper grants no launch permission. Normal paced selection includes the existing pending-bootstrap path; the explicit `--ignore-quota` branch uses `_ignore_quota_verdict`, including its over-pace and telemetry-outage permissions. Do not substitute `Provider.available` for either branch's existing rules. Allow at most one observation-triggered selection per local tick interval; only an actual scheduled deadline can wake sooner. This bounds persistent mismatches and self-written observations without delaying ordinary due refreshes or imposing a launch gate.
5. If the new state remains blocked, schedule the earliest of the original normal deadline and newly derived deadlines for providers with validated changed local state. Include that identity's scoped cooldown expiry, exhaustion/reset or eligibility boundary, and relevant model-refusal/bootstrap expiry. A cache-only miss, invalid entry, or local-unavailable result preserves that provider's previous absolute deadline and refresh permission; never derive a new `POLL` deadline from a synthetic unavailable sibling verdict. For example, a token change four minutes into a one-hour refusal must be re-evaluated when an applicable fifteen-minute source-scoped pause expires, rather than waiting out the original hour. Repeated peer writes cannot extend the original deadline.
6. If a changed credential has no usable cached observation, permit a refresh only when the existing identity-specific HTTP policy allows it. Preserve a per-provider attempt/deadline state across outer-loop iterations, including failures, so the same missing or malformed sample cannot trigger an attempt on each tick. Coalesce Claude fetches through the existing mechanism.
7. When the original scheduled refresh becomes due, perform the existing normal recheck. Neither failed reads nor successful but still-blocked updates may postpone it. Recompute wall-clock boundaries without repeatedly resetting the monotonic wait budget. Consume a due deadline after its ordinary refresh attempt and derive the next retry from the resulting policy; do not repeatedly wake on a deadline already served.

Make per-provider refresh control and a genuinely cache-only provider-read mode explicit requirements. A Claude-only wake must not run `choose_model(refresh=True)` for both providers, and `refresh=False` alone is insufficient because cache misses can still fetch. An unchanged sibling with an expired cache remains in its normal scheduled refresh path; an early wake does not create permission to fetch it. Existing callers keep their current default behaviour.

Claude has shared usage caching; Codex currently has per-worker caches. Preserve that distinction rather than promising shared Codex observations. When several workers detect a change, test both a cacheable Claude response and a winner response with idle or malformed windows that cannot supply a reusable cache. At most one early attempt per worker and observed identity/recovery event is permitted before returning to the existing normal retry schedule; reaching the original scheduled recheck must still work. This bookkeeping is private to a wait episode: it starts at the first blocked selection, survives observation-triggered outer-loop iterations, and ends at a launch or an ordinary refresh at the pinned original deadline. An earlier policy-derived retry consumes its own due deadline and can schedule its next ordinary retry within that original bound. Attempt bookkeeping limits only extra HTTP attempts: it cannot block validated cached recovery, ordinary scheduled refreshes, or otherwise-authorized launches. Add small bounded jitter only to newly advanced network attempts, within the detection budget, to avoid simultaneous bursts. Do not delay cache-backed recovery, introduce a new mandatory lease, or treat jitter as proof of request coalescing. Compare aggregate attempts against the existing scheduling policy for these cases before accepting the implementation.

Use existing signal handling so stop/restart remains responsive. Avoid filesystem watcher dependencies, background threads, and new persistent leases. The change should add opportunities to resume work without making successful local observation a launch prerequisite.

### 4. Preserve compatibility and useful status

- Keep custom `--quota-cmd` execution on its existing schedule; never invoke it on every local tick. With a custom command, sleep to its existing deadline without inspecting unrelated local Claude/Codex records. This change does not invent a cache contract for external commands.
- Keep GitHub quota waits, API funding waits, fleet-capacity admission, no-progress backoff, peer-claim waits, and active model sessions outside this change.
- Keep `next_action_at` as the scheduled normal recheck. The manager uses this field when deciding whether it can reload a worker's read policy; setting it to a local tick and then leaving it stale can defer reloads indefinitely. Report local-check cadence and provider HTTP retry deadlines separately, and let displays derive local-check timing without a durable write on every tick. Test manager reload behaviour while the worker is waiting.
- Record meaningful wait entry, recovery, and normal-refresh events. Avoid a log line or durable status write on every unchanged tick; retain existing supervisor heartbeats.
- Extend the existing usage request journal only if needed to verify behaviour. Logging failure remains non-blocking.

## Verification

Use deterministic clocks, simulated local changes, and counted fake HTTP requests. Do not send live requests to provoke a 429. Extend the quota loop, shared usage, cache identity, hard-block, and outage-autonomy tests already in the repository.

| Scenario | Required result |
| --- | --- |
| One-hour cooldown, unchanged credentials and cache | Local checks continue; zero extra HTTP requests before the original retry deadline; normal recheck still occurs when due. |
| Changed credential plus a valid available shared observation | Worker leaves quota wait within 30 seconds, then passes normal selection and launch checks. |
| Change after blocked selection but before wait entry | Immediate entry observation catches the change; it is not accepted as the unchanged baseline. |
| Persistent source/mirror or primary/fallback disagreement | Observer signatures are compared consistently; at most one observation-triggered selection per tick, with no tight selection/log/renewal loop. |
| Token renewal for an identifiable rate-limited account | The same account's cooldown remains binding; no early endpoint request. |
| Changed credential with only source identity available | Existing bounded source-scoping rules remain intact; the old credential's full cooldown is retained. |
| Source-scoped pause expires after an early token-change observation | An unchanged subsequent signature still wakes at that earlier absolute expiry. |
| Repeated peer writes while still blocked | No update postpones the original normal refresh deadline or existing hourly revisit. |
| Another worker publishes a newer valid Claude observation, while Codex's cache is expired | Waiting peers notice it within 30 seconds; Codex fetch times through its original deadline match the unchanged policy, rather than moving to a new `now + POLL` schedule. |
| Read-only credential mount and fresh `Quota` instance | Cache, cooldown, and refusal records are found in the fallback without writes or locks; conflicting old primary records are reconciled correctly. |
| Isolated home with a changed source token | Source change is noticed without rewriting the mirror on ticks; normal selection synchronizes the mirror before authorization. |
| Old over-pace observation, expired sample, crossed reset, wrong identity, malformed or future-dated observation | No launch permission is manufactured from the entry. |
| Authentication refusal, known exhaustion, model refusal, unopened Claude window | Existing launch restrictions and bootstrap reservation rules remain unchanged. |
| Explicit `--ignore-quota`, including a hard-to-soft transition | Existing permitted outage autonomy and over-pace recovery continue; known hard limits still apply. |
| Missing/unreadable cache, injected unexpected exception, temporary credential lookup failure, unsupported observation source | Existing normal recheck is preserved; no crash, busy loop, or permanent observation-dependent wait. Failure followed by the same successful observation does not count as a change. |
| Same-timestamp replacement, wall-clock adjustment, or restart during a wait | Changes are detected correctly and persisted restrictions remain effective. |
| Several workers recover, with cacheable and uncacheable Claude responses and a Codex credential change | Count aggregate attempts; no repeated early attempts for the same event, no additional polling of unchanged providers, and no bootstrap calls from ticks. |
| macOS locked or slow Keychain | No Keychain subprocess or repeated credential warning on local ticks; the normal bounded lookup schedule is preserved. |
| Manager reload during a long quota wait or just as a tick starts a round | `next_action_at` does not prevent reload indefinitely; existing child/idle checks protect a round that has already started. |
| Forced provider, auto selection, custom quota command, stop signal | Existing provider restrictions and command cadence hold; stop remains prompt. |

Replace assertions that require a single `sleep(3600)` with elapsed-time and call-count assertions: zero refreshes before the due deadline in an unchanged-state test, then one ordinary recheck. Advance both wall and monotonic clocks with the injected sleeper; a fake sleep that advances neither must not hang the suite.

During local ticks, make HTTP, credential renewal, credential mirroring, shared-directory creation, fetch-lock acquisition, and subprocess execution fail the test if invoked. Inject ordinary observer exceptions and stop exceptions separately to prove fallback and signal propagation. Include the normal selection and launch path in recovery tests so a cache-only helper cannot pass while the outer loop still refreshes the unchanged sibling.

Compare request counts with the unchanged implementation for identical unchanged-state and unsuccessful-recovery timelines. The local tick path must issue zero HTTP requests; network requests caused by an actual recovery or launch remain subject to the normal provider policy. Use the existing request journal for rollout evidence. Run the relevant quota integration scripts and the repository's required checks after the focused regressions pass.

## Review and deployment verification

Obtain an independent review focused on throughput, autonomous recovery, credential/cooldown identity, refresh frequency, and failure fallback. Require green CI and resolve substantive findings before merging.

Roll out at an idle boundary without interrupting paid work, preserving worker configuration. Verify installed revision and running process provenance. Observe a natural wait and recovery, or use an isolated fixture with fake credentials and telemetry when a natural event is unavailable. Never rotate personal credentials, edit live cooldowns, or force endpoint rate limits for the demonstration.

Acceptance requires evidence that supported file/cache recovery is noticed within 30 seconds, applicable HTTP cooldowns are honored, original refresh deadlines cannot be postponed, unchanged-state request frequency has not increased, and failed recovery does not create a new polling loop. Verify all seven workers remain supervised and ordinary work can proceed through the existing launch path. Test container fallback, isolated homes, and macOS behaviour in fixtures as well as the host fleet path. Report unrelated constraints, such as GitHub REST exhaustion, separately so they do not obscure whether quota recovery worked. If live recovery has not occurred, report deployment as installed but behaviour awaiting live confirmation.

Rollback by restoring the previous reviewed worker release at an idle boundary. No cache migration or cooldown deletion should be needed. Update `docs/quota.md` with the implemented scheduling behaviour and its distinction from HTTP retry timing.

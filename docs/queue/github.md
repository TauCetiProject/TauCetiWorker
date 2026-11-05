# The queue's GitHub adapters and Bors-compatible surface

Stream F of the [AI merge queue plan](../redesign/specs/ai-merge-queue.md): how the queue service
talks to GitHub and how it stays a drop-in replacement for Bors's approval intake, labels, statuses,
comments and observation feeds. The modules:

| Module | Content |
| --- | --- |
| `tauceti_queue/github_app.py` | App credentials, the RS256 App JWT, installation tokens refreshed before expiry, the TLS context, `GitHubApp.load` |
| `tauceti_queue/github_adapter.py` | the one rate-limit-aware client, the `GitHubPort` adapter (`ports.py` records), the polling fallback |
| `tauceti_queue/diagnostics.py` | source and infrastructure findings of a completed candidate run |
| `tauceti_queue/commands.py` | Bors-compatible command parsing and execution with admission evidence |
| `tauceti_queue/eligibility.py` | the review App's `merge eligibility` checks, the way TauCetiReview approves today |
| `tauceti_queue/webhook.py` | signature verification, delivery deduplication, routing |
| `tauceti_queue/approval_poll.py` | the periodic reconciliation of `merge eligibility` checks behind the webhooks (`docs/queue/operations.md`, "Approval polling") |
| `tauceti_queue/bors_import.py` | the cutover import of Bors's approved PRs and running head from its feeds (`docs/queue/controller.md`) |
| `tauceti_queue/feeds.py` | the two Bors feeds over the controller's records, the schema fixture derivation and validator |
| `tauceti_queue/server.py` | `create_server(controller, config, status=...)`: health, status, feeds, webhook, worker and admin endpoints |

Tests: `tests/queue_commands.py`, `tests/queue_eligibility.py`, `tests/queue_diagnostics.py`,
`tests/queue_github_adapter.py` (fake transport with recorded responses), `tests/queue_feeds.py`
(generated feeds against the committed schema, including feeds over the real controller and the
end-to-end fakes), `tests/queue_server.py` (loopback server with a fake controller). All run from
`tests/run-all`.

## App authentication

Credentials live in `~/.config/tauceti-queue/` (override with `TAUCETI_QUEUE_CONFIG` or an explicit
path): `github-app.json` with `id`, `slug` and `installation_id`; `github-private-key.pem`;
`github-webhook-secret`. `AppCredentials.load` refuses an incomplete directory; `GitHubApp.load
(credentials_dir)` is what the entrypoint calls and what `GitHubAdapter(app, repository,
clone_dir=...)` is built from. The App JWT is built with `cryptography` alone (header, claims `iat`
backdated 60 s, `exp` 9 minutes ahead, `iss` the App ID, PKCS#1 v1.5 SHA-256), and
`InstallationTokenSource` exchanges it for an installation token that is cached and refreshed five
minutes before GitHub expires it; a 401 on an installation call invalidates the cache once and
retries. Neither the key nor a token is written to disk or logged. `ssl_context()` loads
`/etc/ssl/certs/ca-certificates.crt` when present, as `tauceti_worker/github.py` does, because the
uv-managed interpreters on the fleet lack the system bundle.

Live check run on 2026-11-04 with the stored credentials, read-only: `GET /app` returned App 5607007,
slug `tauceti-queue`, owner `TauCetiProject`, permissions `actions: write, checks: read, contents:
write, issues: write, metadata: read, pull_requests: write, statuses: write, workflows: write` and
the eight subscribed events; `GET /installation/repositories` with an installation token returned
`TauCetiProject/TauCeti` only; the installation's primary limit is 6350 requests per hour. No write
was made.

## The `GitHubPort`

`GitHubAdapter` implements `tauceti_queue/ports.py` (stream E's authoritative protocol): timestamps
are unix seconds, run and comment ids strings, `repo` is `owner/name`, SHAs are full.

| Method | Behaviour |
| --- | --- |
| `fetch_pr(repo, number) -> PullRequest` | `GET /repos/{repo}/pulls/{n}`; the port's record plus `base_sha`, `author_id`, `head_repo`, `merged`; raises `PullRequestNotFound` (a `LookupError`) for an unknown number |
| `fetch_refs(repo, refspecs)` | `git fetch` into the configured clone of `https://github.com/{repo}.git`; the installation token travels as an `http.extraheader` Authorization header through `GIT_CONFIG_COUNT`/`GIT_CONFIG_KEY_0`/`GIT_CONFIG_VALUE_0`, so it is in neither argv, `.git/config`, nor any log |
| `dispatch_candidate(repo, *, head_sha, base_sha, base_ref, batch_id, members) -> str` | `POST /repos/{repo}/dispatches` with `event_type: tauceti-bors-staging` and `client_payload {head_sha, base_sha, batch_id, members, base_ref}`, what Bors sends; `members` entries are `{pr, head_sha}` when given as mappings and `{pr}` for bare numbers (see findings); returns the run title `bors branch=<b> batch=<id> head=<sha> base=<sha>` as the dispatch id |
| `find_candidate_run(repo, *, batch_id, head_sha, base_sha, base_ref="main", since=None) -> CandidateRun \| None` | one list call `GET /actions/workflows/pr-build.yml/runs?event=repository_dispatch&created=<since-60s>..<now+60s>` matched on `display_title`, then the attempt's jobs; `phases` are the jobs and their steps as `CandidatePhase(name, started_at, completed_at, status, detail=conclusion)`; `runner_name`, `runner_labels` and `host` come from the `sandboxed-build` job; a completed unsuccessful run carries `diagnostics` (below); two runs with the same title raise `AmbiguousCandidateRun`, except that with `since` (the controller passes the attempt's dispatch time) the newest run created at or after it answers, because an infrastructure re-dispatch of the same head, base and batch legitimately creates a second run with the same title |
| `commit_status(repo, sha, context) -> Status \| None` | the latest status for `context` from `GET /commits/{sha}/status`, with `run_id` parsed from `/actions/runs/<id>` in the target URL |
| `fast_forward(repo, branch, expected_parent, tip) -> FastForwardResult(ok, actual_parent, error)` | reads `refs/heads/<branch>`; if it is not `expected_parent`, reports the actual parent without writing; else `PATCH /git/refs/heads/<branch>` with `force: false`; a 422 (non-fast-forward) re-reads the ref and reports it as a moved parent with GitHub's message |
| `update_ref(repo, ref, sha)` | force-updates `refs/<ref>` by pushing `+<sha>:refs/<ref>` from the service's clone with the token in the `GIT_CONFIG_*` header, so the retained commit's objects (which exist only in that clone) travel with the ref; without a clone the Git Data API creates or moves the ref, which works only for objects GitHub already has (retained candidate refs, never the base branch). The controller pushes the head's retained ref this way before every dispatch, since CI checks the candidate out by SHA |
| `post_comment(repo, number, body, marker) -> str` | appends `<!-- tauceti-queue <marker> -->`; a comment already carrying the marker is edited (or left alone when identical), never duplicated |
| `set_labels(repo, number, add, remove)` | `POST /issues/{n}/labels` and `DELETE /issues/{n}/labels/{name}` (404 tolerated) |
| `close_pr(repo, number, title)` | `PATCH /pulls/{n} {state: closed, title}`, bookkeeping fields only, as Bors's `close_merged_pr`; the controller passes the `[Merged by Bors] - ` title |
| `repository_admin(login, repo=None) -> bool` | `GET /collaborators/{login}/permission` is `admin` (the `security_audit.require_admin` predicate); `reviewer(login)` is Bors's reviewer check, push access (`admin`, `maintain`, `write`) |
| `rate_limit() -> (int, float)` | from the last response headers, else `GET /rate_limit` |

Also used by the server, the command runner and the eligibility reader: `fetch_pr_or_none`,
`pr_merge_base`, `collaborator_permission`, `eligibility_checks`, `labels`, `user_for_token` (the
worker endpoints' bearer token resolved through `GET /user`), `post_commit_status`, `workflow_run`,
`job_log`, `app`, `installation_repositories`. The approval poll's listing is
`list_open_pull_requests(repo, review_app_id=)`: one GraphQL query per 100 open PRs (`POST /graphql`,
on the GraphQL quota, whose headers are kept out of the REST counter) returning each PR's number,
head, base, draft flag and the review App's `merge eligibility` check runs from the head commit's
check suite (`checkSuites(filterBy: {appId})`), mapped to the REST `check_runs[]` record shape so
`eligibility.latest` reads them unchanged.

All calls go through `GitHubClient`, which records the primary limit from every response, waits on
secondary limits (`Retry-After`, or `X-RateLimit-Remaining: 0` until the reset, at most 120 s, at
most four attempts) and retries 5xx with backoff. `client.stats()` (requests, retries, limit waits,
failures, remaining, reset, last error) is part of `/status` as `github`.

### Dispatch, polling and verdict binding

A candidate build is the `repository_dispatch` above. `pr-build.yml` derives its `run-name` from the
payload, so the run is found by that title, with the creation window bounded to a minute either side
of the dispatch time; this is the same lookup Bors's `staging_run` performs. The verdict is the three
commit statuses Bors required (`bors.toml` `status = ["build", "bump-guard", "scope"]`,
`VERDICT_CONTEXTS`) that `finalize-merge-group-build` posts on the candidate head in one step through
TauCetiTools' `ci_status.py`, each with a `target_url` of exactly
`https://github.com/<repo>/actions/runs/<run_id>`; `Status.run_id` carries that id so the controller
accepts a status only when it names the candidate's own run (a status from another run, an earlier
attempt, or a `pull_request` event on a member branch is not the candidate's verdict). The attempt
passes only when all three are bound and `success`; a red `scope` or `bump-guard` beside a green
`build` is a source failure of the head carrying the status description (a restricted path without a
current admin approval, an oversized diff, a new file over 1000 lines, a failed pin validation), and a
bound `build` with the other two still absent is observed again for `LOG_GRACE_SECONDS` before it
counts as a missing status. `status.head.ci.verdicts` reports each context's state.

Webhooks come first. `CandidatePoller.poll(refs)` is the fallback: for each pending candidate at most
one run lookup and one status read per `interval` (60 s), and nothing at all while the remaining
quota is below `reserve` (200). The controller's own loop applies the same bound per CI attempt.

### Diagnostics of a failed run

`CandidateRun.diagnostics` is filled for a completed run whose conclusion is not `success`: the
`sandboxed-build` job's log (`GET /actions/jobs/<id>/logs`, a redirect to a signed URL fetched
without credentials, at most 64 MB) is read with bors-ng's `priv/failure_diagnostics.py` patterns
and mapped to the controller's records, dicts with `kind`, `module`, `message` and, where known,
`path`, `line_number`, `column`, `declaration`, `linter`:

| log line | kind |
| --- | --- |
| `TauCeti/A/B.lean:12:3: error: ...`, `error: TauCeti/A/B.lean:12:3: ...` | `compiler` on `TauCeti.A.B` |
| `audit-driver: import TauCeti.X failed, environment already contains '...' from TauCeti.Y` | `audit` on both modules |
| `duplicate-declarations: name is declared in both M and N` | `audit` on the project modules |
| `LINT-ENV-LOCATIONS {...}` (the environment linter's JSON) | `lint` on the defining path, `unknown` kept |
| `Some required targets logged failures:` then `- TauCeti.A.B` | `compiler` per target, only when nothing was located |

Infrastructure findings (`kind == "infrastructure"`, with a `reason`) come from the run and jobs: a
`cancelled`, `timed_out`, `startup_failure`, `action_required` or `stale` run, a cancelled or
timed-out job, a failed `finalize-merge-group-build` job (the `build` status may be missing), a lost
runner named in the log, an unavailable log, or a completed failure with no status published. The
controller uses those alone for classification and hands the rest to the repair task unchanged.

## Admission: how approvals arrive

Bors takes approvals from three sources; the queue takes the same three.

**Comment commands.** `bors r+`, `bors merge`, `bors r=login`, `bors p=N`, `bors r-`, `bors cancel`
in issue comments and review bodies, from collaborators with push access, parsed below.

**The review App's eligibility checks.** TauCetiReview no longer comments `bors r+`: its reusable
`merge-only.yml` and `merge-sweep.yml` run `runner/backend.py eligibility`, which publishes an
App-owned check run named `merge eligibility` on the reviewed head (App 3947238,
`tauceti-review-bot[bot]`). The check's `external_id` is the decision, `{"schema":
"tauceti-merge.eligibility/v1", "repo", "pr", "merge_base_sha", "eligible", "review_safe",
"single"}`, with conclusion `success` (eligible), `failure` (review unsafe, approval withdrawn) or
`neutral` (waiting). Bors consumes it in `lib/worker/merge_eligibility.ex`; `eligibility.reconcile`
does the same on every `check_run` delivery of such a check: the trusted check with the highest id
on the live head decides (listed with `filter=all`, so another App's check of the same name cannot
hide it); an eligible decision approves at the live head with reviewer `tauceti-review-bot[bot]`,
`single` recorded, and the check as evidence, once the live merge base equals the reviewed one, the
PR is open, not a draft, on the canonical branch and carries no keep label (`keep`, `hold`, `wip`,
`human`, `do-not-close`); an unsafe review unapproves; neutral and ineligible decisions change
nothing. The checks describe eligibility independently of `MERGE_BACKEND`. Behind the deliveries,
`approval_poll.ApprovalPoller` lists the open PRs' checks every `approval_poll_interval_seconds` and
reconciles the decisions the controller has not recorded (`docs/queue/operations.md`, "Approval
polling"), so an inactive or lossy webhook cannot lose an approval or a revocation.

**The review App's exact-head comments.** `bors r+ sha=<head>`, `bors r+ single sha=<head>` and
`bors r- sha=<head>` are still accepted from comments whose `performed_via_github_app.id` is the
review App, and only while the live head is that SHA, as Bors's webhook accepts them (the legacy
path of the same App).

Every approval reaches the controller as `approve(repo, pr, head_sha, reviewer, priority,
evidence)` with the evidence as a dict (`commands.Evidence`): the source kind (`issue_comment`,
`pull_request_review`, `review_app`, `eligibility_check`), actor login and id, the comment, review
or check id and URL, its time, the head approved, the command as typed, `single`, `admin`, and for
repository admins the merge base of that head against the base branch, which is what Bors's
`ManualApproval.grant` records and what the scope-approval check reads back as `scope_approvals`.

## Commands

The grammar follows bors-ng's `COMMAND_PARSING.md`. A line is a command line when it starts with
`bors` (any case), an optional `:`, and whitespace; each line of a comment is read on its own; runs
of whitespace read as one space; a command word running into a longer word is prose and reads as
nothing (`bors merged this`, `bors cancelled it`, `bors single-handedly`).

| Line | Effect |
| --- | --- |
| `bors r+`, `bors merge` | `controller.approve(repo, pr, head, commenter, 0, evidence)` |
| `bors r+ p=N`, `bors merge p=N` | approve with priority `N` (a 32-bit integer) |
| `bors r+ single on\|off` | approve; `single` is recorded in the evidence but has no effect, the queue has no singleton builds |
| `bors r=login[,login]`, `bors merge=...` optionally `p=N` | approve with the named reviewer; the evidence names the actual commenter |
| `bors p=N` | `controller.set_priority(repo, pr, N)` |
| `bors r-`, `bors merge-` | `controller.unapprove(repo, pr, commenter)` |
| `bors cancel` | `controller.cancel(repo, pr, commenter)` |
| `bors single on\|off` | accepted without effect |
| `bors ping` | `:pong: pong` |
| `bors try`, `try-`, `retry`, `delegate`/`d` forms, `link`, `stack`, `unlink` | refused with a reply saying the merge queue does not run them |

Anything after a complete command refuses the whole line with Bors's reply shape (`:-1: bors did not
run \`bors r- now\`: ...`); a modifier that cannot be read (`bors r+ p=abc`) swallows its command; a
line that could mean two things (`bors r=alice bob`) is refused, not guessed; a miscased or unspaced
command (`bors R+`, `bors r +`, `bors:r+`, `bors +r`) gets `Did you mean \`bors r+\`?` and runs
nothing. No reply line begins with the trigger.

Permission comes first: a comment needs the level of its most demanding command or reply (`ping`
none; `r-`, `merge-`, `cancel` member; everything else reviewer; unknown refusals fail closed at
reviewer). Bors's reviewer set is the collaborators with push access, so both member and reviewer
resolve to `adapter.reviewer(login)` (`admin`, `maintain` or `write`); the review App is a reviewer
by identity. Someone short of it gets one `:lock: Permission denied` reply. A draft then refuses
every merge-bound command (`r+`, `p=`, `single`, the refusals and corrections of those) with one
reply and runs the rest (`r-`, `cancel`, `ping`). Replies are posted with marker
`reply <kind>:<comment id>[:<line>]` so a redelivered webhook edits rather than repeats them.

Not carried over from Bors: delegation and the merge-time delegation check, `link`/`stack` bundles,
`try` builds, the project-level reviewer list (GitHub's collaborator permission is the source), and
the dashboard link in the permission-denied reply.

## Webhooks

`POST /webhook/github` verifies `X-Hub-Signature-256` (HMAC-SHA256 with the stored secret over the
raw body; 401 otherwise), deduplicates `X-GitHub-Delivery` over the last 4096 deliveries, ignores
repositories the service does not serve, answers `ping`, and for every subscribed event
(`pull_request`, `pull_request_review`, `issue_comment`, `status`, `check_run`, `check_suite`,
`push`, `workflow_run`) calls `controller.handle_event(kind, payload)` after first running the
commands of a created `issue_comment` on a pull request or a submitted `pull_request_review` with a
body, or reconciling the eligibility of the PR a `merge eligibility` `check_run` names. A failure
inside the controller answers 500 so GitHub redelivers.

## `MERGE_BACKEND`

The TauCeti repository variable `MERGE_BACKEND` (`queue` when unset) is read in these places, and
every one of them distinguishes only `bors` from anything else:

| Reader | Under `bors` | Otherwise |
| --- | --- | --- |
| `pr-build.yml`, `retry-ci-status.yml` (`CI_STATUS_MERGE_BACKEND`, TauCetiTools `ci_status.py::scope_approved`) | a restricted-path waiver is verified against the observation feed (`bors_scope_approval.verify`), the `build` status says `no valid current bors scope approval` when it fails | the waiver fails with `restricted paths need an administrator's admin merge` |
| `queue-release.yml` | skipped (`vars.MERGE_BACKEND != 'bors'`) | wakes the GitHub merge queue after a Lake-pin PR leaves it |
| TauCetiReview `merge-only.yml` / `merge-sweep.yml` (`runner/backend.py`) | publishes the `merge eligibility` check and leaves admission to Bors (`sweep`: `admit=mode == "bors"`); drain checks against the GitHub merge queue are skipped | `queue`: enqueues into the GitHub merge queue after `backend.allow` sees Bors's `batch_ids` empty; `direct`: merges the reviewed head directly |
| bors-ng `MergeReconciler` and the Cloudflare controller | Bors admits | Bors defers to the GitHub queue |
| TauCetiTools `doctor.py`, `project.py` | onboarding expectations only | |

TauCetiReview's runner reads the variable through the GitHub API at each run; nothing in
TauCetiReview, TauCeti's workflows or TauCetiTools writes `bors r+` comments any more (the review
App's comment form is legacy, still recognised). So the service runs with `MERGE_BACKEND` left at
`bors`: it accepts the same commands, reads the same eligibility checks, dispatches the same
`repository_dispatch`, reads the same `build` status and serves the same feeds, and no workflow path
changes behaviour. The cutover is not a variable change: Bors stops receiving deliveries (suspend the
`tau-ceti-bors` installation and point `bors.taucetiproject.org`, which the scope check and the
workers read, at the service), and the service's `publication_enabled` is switched on
(`operations.md`, "Cutover"). No workflow path misbehaves with the service acting as `bors`; the one
path that reads the feed with a precondition is the scope-approval check (findings below).

## Feeds

Served at the same paths as Bors, for repository id 1 = `TauCetiProject/TauCeti` (configured in
`ServerConfig.repositories`; `/api/observation` is an alias for the first repository's observation
feed, the path the status document names):

- `GET /repositories/1/active-batches?base=main[&batch_id=N]`: `tauceti-bors.observation/v1`
- `GET /repositories/1/candidates?base=main[&batch_id=N][&promoted=true]`: `tauceti-bors.candidates/v1`

When `base` is absent the service uses the repository's canonical branch, where Bors answered with
`base: null` across all branches; TauCetiTools and the workers always pass `base=main`.

`feeds.feed_source(controller)` is the view the generators read: the controller itself when it
implements `FeedSource`, else `ControllerFeedSource`, which reads the controller's store (`inputs`,
`batches`, `members`, `ci_attempts`, `journal`) and maps its states to Bors's: the open batch is
`waiting`, the head `running` while a CI attempt is pending and `waiting` otherwise, a landed batch
`ok`, a removed one `canceled`; a candidate is one CI attempt (generation = batch version); an
admission is one member row with its input's approval evidence.

### The schema fixture

`tests/fixtures/bors-feeds-schema.json` is derived by `python -m tauceti_queue.feeds derive
<active-batches.json> <candidates.json>` from the saved production responses
`active-batches-2026-11-04T0745Z.json` and `candidates-latest.json` (`~/tmp/bors-journal`): for every
path the JSON types seen, for every object its key set and the keys present in every instance, for
every list its item shape. `candidates[].events[].details` is kind-specific and recorded as an open
object. Lists that were empty in every saved response (`held`, `bump_repairs`,
`handoff.released_github_prs`) take their item shape from Bors's `api_controller.ex` and the readers.
`requested_batch`, always null in the saved responses, is linked to the `batches[]` record that
Bors's `batch_detail` serves there. Four paths are widened beyond what was observed, each for a
documented reason below (`batch_id`, `runner_*_at`, `unknown_reason`). The fixture validates the
two source files and the older `candidates-2026-11-04T0416Z.json` with zero errors, and
`tests/fixtures/bors-feeds-sample.json` is a 20 KB cut of the sources that the tests validate on
every machine. `feeds.validate` reports a wrong type, a missing required key, an unknown key, a
changed schema string or timestamp unit, so drift in either direction fails `tests/queue_feeds.py`.

### Observation feed, field by field

| Field | Source in the queue | Compatibility |
| --- | --- | --- |
| `schema`, `repo`, `base` | constants and the query | identical |
| `batch_ids`, `batches[]` | the open batch and the head, newest first | identical shape; `state` is `waiting` for the open batch and `running` for the head under CI, which the scope-approval check requires |
| `batches[].id/base/sealed/head_sha/base_sha/members[]{pr, head_sha}` | the batch record; members with their accepted heads | identical; the open batch has null `head_sha` and `base_sha`, as Bors's waiting batches do |
| `batches[].promotion_retry_at`, `promotion_failures`, `recovery_after`, `recovery_failures` | 0 unless the controller records them | same ints; the queue's recovery is the repair task |
| `batches[].train` | `{candidate_id, generation, predecessor_id: null, staging_ref}` for a batch with a CI attempt (`staging_ref` is the branch `staging-candidate-<candidate_id>-<generation>` the service pushes at dispatch with the head as its tip, the name `pr-build.yml`'s train identity check fetches and compares with the dispatched head; the candidate id is the attempt id, the generation the batch version), null otherwise | identical; `predecessor_id` is always null, there are no speculative trains |
| `held` | always `[]` | Bors held bundle members; the queue has no bundles |
| `bump_repairs` | always `[]` | Bors's bump-repair handoff, read by `tauceti_worker/github.py::bump_repair_requested`; the repair task replaces it, so the worker finds no handoff and leaves the PR to a later round, the same outcome as an empty list today |
| `outcomes[]{pr, head_sha, batch_id, state}` | per member revision, newest batch first, one per `(pr, head_sha)` | identical |
| `scope_approvals[]{pr, actor_id, head_sha, merge_base_sha}` | admin approvals recorded with their merge base | identical; checked by `bors_scope_approval.approved` in the tests |
| `requested_batch` | the `batch_id` batch when it is on `base`, else null | identical to a `batches[]` record |
| `handoff` | a block with Bors's twelve keys: `backend: "queue"`, `pending: 0`, `reason: "queue_service"`, `eligible: []`, `github_count: 0`, `bors_count` the number of active batches, `candidate_scan_complete: true`, `overlap: false`, `updated_at` the service start, `observed_at` now | Bors's merge-backend handoff state; no reader outside Bors consumes it |

### Candidates feed, field by field

| Field | Source in the queue | Compatibility |
| --- | --- | --- |
| `schema`, `repo`, `base`, `timestamp_unit` (`unix_microseconds`), `historical_unknown_reason`, `queue_authority: false` | constants | identical |
| `batch_id` | the query's `batch_id` or null | widened to int or null; the saved responses had no `batch_id` query |
| `candidates[]` (100 most recent) `id/batch_id/generation/observed_at` | the CI attempt: its id, batch, batch version, dispatch time | identical |
| `candidates[].snapshot{base, base_sha, contract, members, previous_generation_id, speculative_predecessor_id}` | the batch's members and the attempt's base | identical; `contract` is `tauceti-bors.candidate/v1`, `speculative_predecessor_id` always null, `previous_generation_id` the previous attempt of the batch |
| `snapshot.members[]` | one per member, every Bors key | `pr`, `head_sha`, `assembly_head_sha`, `reviewer`, `original_admitted_at` (microseconds of the approval), `manual_approval` (admin evidence), `scheduling.priority` from the records; `patch_id` is the PR number, `admission_id` and `link_id` the member row id (Bors's are its own row ids), `bundle_id` null, `queue_age_retries` and `remainder_retries` 0, `retry_reason` and `parent_*` null, `scheduling.singleton` the `single` flag, `scheduling.policy` and `label_hint.source`/`binding` descriptive strings, `label_hint.labels` the labels in the evidence with `observed_at` in unix seconds, `source` `queue.admission` |
| `candidates[].events[]{id, kind, source, observed_at, details}` | the controller's journal entries for the batch, source `queue.controller` | identical shape; the kinds are the controller's (`dispatch`, `ci-pass`, `promoted`, ...), `details` is the journal detail |
| `candidates[].runner_assigned_at/started_at/finished_at` | from the run's creation, start and completion, microseconds | widened from always-null: Bors never had them |
| `candidates[].unknown_reason` | null once the lifecycle is known, else a string | widened from always-string |
| `admissions[]` (500 most recent) `{id, batch_id, link_id, observed_at, snapshot}` | one per member row, newest first | identical; the snapshot has the member keys without `admission_id`, `link_id` and `assembly_head_sha`, as Bors's admission snapshots |
| `promoted=true` | attempts of landed batches, no admissions | as Bors |

## Server

`create_server(controller, config, *, status=None, adapter=None, app=None)` returns a
`ThreadingHTTPServer` (daemon threads, `serve_forever()`/`shutdown()`) sharing the controller object
with the controller loop. `config` is either a `ServerConfig` or the service `Config`
(`tauceti_queue/config.py`): then the listen address is `listen_host:listen_port`, repository id 1 is
`config.repository`, the webhook secret comes from the loaded `GitHubApp` or from
`credentials_dir/github-webhook-secret`, and the adapter is the one passed or the controller's
`github`. `status` is the entrypoint's status document provider (`Service.status_document`, the
`tauceti-queue.status/v1` envelope around `controller.status()`), served at `/status` with the
`github` client counters and `webhooks` counters added; without it `/status` is the controller's
status plus `revision` and `started_at`.

| Route | Behaviour |
| --- | --- |
| `GET /health/`, `/health` | `200 ok` |
| `GET /healthz` | `{ok, schema, service, revision}` from the status document, for the tunnel check |
| `GET /status` | the status document |
| `GET /settings` | `{settings: <effective>, source: "controller"}` (what `tauceti-queue settings show` reads) |
| `GET /repositories/<n>/active-batches`, `/candidates`, `/api/observation` | the feeds |
| `POST /webhook/github` | the receiver |
| `POST /tasks/claim` | bearer; `claim_task(worker_identity, github_login)`; the `Task` as JSON or 204 when nothing is claimable |
| `POST /tasks/<id>/renew` | bearer; body `{claim_token}`; `renew_task(task_id, claim_token)`; `{task_id, expires_at}` |
| `POST /tasks/<id>/result` | bearer; body `{claim_token, tip_sha, worker_notes}`; `submit_task(...)`; the `Adoption` as JSON |
| `POST /tasks/<id>/release` | bearer; body `{claim_token, reason, error}`; `release_task(...)`; 204 |
| `POST /tasks/inference` | bearer; body `{worker, ok, error}`; `report_inference(...)`; 204 |
| `POST /admin/settings` | bearer and repository admin; `set_setting(key, value, login)` per key; `{settings, actor}` |
| `POST /admin/urgent` | bearer and repository admin; body `{repo, pr}`; `mark_urgent(repo, pr, login)` |
| `POST /admin/retry-ci` | bearer and repository admin; `retry_ci(repo, login)` |

Bearer tokens are GitHub tokens resolved to a login through `GET /user` (cached by token digest for
six hours); the admin routes check `repository_admin(login)` before calling the controller, the
predicate `security_audit.require_admin` uses, cached per login for the same six hours. Both caches
keep expired entries and serve them while GitHub cannot answer (a rate-limited 403, 5xx or a network
error, `ports.GitHubUnavailable`), so an exhausted user quota logs nobody out of a claim and grants
nobody new; only a 401 from `GET /user` evicts a token, and a login never checked before gets 503. The worker identity is `body.worker` or
`X-TauCeti-Worker`. A `ControllerError`'s `code` maps to HTTP: `unknown-*` 404, `claim-expired`,
`claim-superseded`, `version-mismatch` 409, `not-authorized` 403, anything else 400; a
`PermissionError` is 403, `LookupError` 404, `ValueError` 400, and any other exception 500 with the
traceback logged. Workers push their result chain to the task's `submission_ref`
(`refs/tauceti-queue/submissions/<task>/<claim>`), which the controller fetches with `fetch_refs`.

## Compatibility findings

- The scope-approval check (`bors_scope_approval.verify` for a batch) requires `requested_batch`
  with `state == "running"`, matching `head_sha` and `base_sha`, and 1 to 128 members, and every
  approval to be exactly `{pr, actor_id, head_sha, merge_base_sha}` by an actor whose live permission
  is `admin`. The feed reports the head as `running` from dispatch to verdict and records the merge
  base at admin approval time (`commands.run_comment`); a head of more than 128 members would fail
  the check for restricted paths, where Bors capped batches at 128.
- TauCetiReview's approvals are `merge eligibility` check runs, not comments; the queue reads them
  (`eligibility.py`). The comment form `bors r+ sha=<head>` the Bors README describes is the review
  App's legacy path and is still accepted by App id.
- `tauceti_worker/github.py` reads only `schema`, `repo`, `base` and `bump_repairs` from the
  observation feed; TauCetiReview's `runner/backend.py` reads `batch_ids`, `batches` (with `base`,
  `state` in `waiting`/`running`, `members`), `held` and `outcomes`, and requires
  `batch_ids == [b.id for b in batches]`; both hold. `work_units.py` links to
  `https://bors.taucetiproject.org/batches/<id>` in a prompt, which the queue does not serve (text
  in an agent prompt, not a read).
- The controller's `dispatch_candidate` port passes `members` as PR numbers; Bors's payload carries
  `{pr, head_sha}`. The adapter emits `{pr}` for bare numbers and `{pr, head_sha}` for mappings, and
  `pr-build.yml` only forwards the list to CI telemetry (`BORS_MEMBERS`), so CI is unaffected; for
  byte-identical payloads the controller should pass `{pr, head_sha}` mappings.
- Bors's `repository_admin` matched the actor by user id after resolving the login; the queue checks
  the login's collaborator permission directly, the same endpoint the scope check uses.
- Bors skipped a notice when any comment since the batch carried its `<!-- bors-outbox key -->`
  marker; the queue edits the marked comment instead, so a retried publication converges on the
  final text rather than leaving the first version.
- The entrypoint (`tauceti_queue/__main__.py`, stream H) is wired here to the controller's
  constructor (`Controller(store, github, clone, repository, revision=..., canonical_branch=...)`),
  `ReconciliationLoop`, `GitHubApp.load`, `GitHubAdapter(app, repository, clone_dir=content_clone)`
  and `create_server(..., status=..., adapter=..., app=...)`; it serves status only while the
  credentials or the content clone are missing, and the status document's `feeds.observation` is
  Bors's path.

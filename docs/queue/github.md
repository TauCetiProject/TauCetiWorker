# The queue's GitHub adapters and Bors-compatible surface

Stream F of the [AI merge queue plan](../redesign/specs/ai-merge-queue.md): how the queue service
talks to GitHub and how it stays a drop-in replacement for Bors's approval intake, labels, statuses,
comments and observation feeds. The modules:

| Module | Content |
| --- | --- |
| `tauceti_queue/github_app.py` | App credentials, the RS256 App JWT, installation tokens refreshed before expiry, the TLS context |
| `tauceti_queue/github_adapter.py` | the one rate-limit-aware client, the `GitHubPort` adapter, the polling fallback |
| `tauceti_queue/commands.py` | Bors-compatible command parsing and execution with admission evidence |
| `tauceti_queue/webhook.py` | signature verification, delivery deduplication, routing |
| `tauceti_queue/feeds.py` | the two Bors feeds, the schema fixture derivation and validator |
| `tauceti_queue/server.py` | `create_server(controller, config)`: health, status, feeds, webhook, worker and admin endpoints |

Tests: `tests/queue_commands.py`, `tests/queue_github_adapter.py` (fake transport with recorded
responses), `tests/queue_feeds.py` (generated feeds against the committed schema), `tests/queue_server.py`
(loopback server with a fake controller). All run from `tests/run-all`.

## App authentication

Credentials live in `~/.config/tauceti-queue/` (override with `TAUCETI_QUEUE_CONFIG` or an explicit
path): `github-app.json` with `id`, `slug` and `installation_id`; `github-private-key.pem`;
`github-webhook-secret`. `AppCredentials.load` refuses an incomplete directory. The App JWT is
built with `cryptography` alone (header, claims `iat` backdated 60 s, `exp` 9 minutes ahead, `iss` the
App ID, PKCS#1 v1.5 SHA-256), and `InstallationTokenSource` exchanges it for an installation token
that is cached and refreshed five minutes before GitHub expires it; a 401 on an installation call
invalidates the cache once and retries. Neither the key nor a token is written to disk or logged.
`ssl_context()` loads `/etc/ssl/certs/ca-certificates.crt` when present, as `tauceti_worker/github.py`
does, because the uv-managed interpreters on the fleet lack the system bundle.

Live check run on 2026-11-04 with the stored credentials, read-only: `GET /app` returned App 5607007,
slug `tauceti-queue`, owner `TauCetiProject`, permissions `actions: write, checks: read, contents:
write, issues: write, metadata: read, pull_requests: write, statuses: write, workflows: write` and
the eight subscribed events; `GET /installation/repositories` with an installation token returned
`TauCetiProject/TauCeti` only; the installation's primary limit is 6350 requests per hour. No write
was made.

## The `GitHubPort`

`GitHubAdapter` implements the method list agreed with the controller stream. `repo` is
`owner/name`; SHAs are full 40-character strings.

| Method | Behaviour |
| --- | --- |
| `fetch_pr(repo, number) -> PullRequest \| None` | `GET /repos/{repo}/pulls/{n}`; `PullRequest(number, head_sha, base_ref, title, body, author_login, state, draft, labels)` plus `base_sha`, `author_id`, `head_repo`, `merged` |
| `fetch_refs(repo, refspecs)` | `git fetch` into the configured clone of `https://github.com/{repo}.git`; the installation token travels as an `http.extraheader` Authorization header through `GIT_CONFIG_COUNT`/`GIT_CONFIG_KEY_0`/`GIT_CONFIG_VALUE_0`, so it is in neither argv, `.git/config`, nor any log |
| `dispatch_candidate(repo, *, head_sha, base_sha, base_ref, batch_id, members) -> str` | `POST /repos/{repo}/dispatches` with `event_type: tauceti-bors-staging` and `client_payload {head_sha, base_sha, batch_id, members: [{pr, head_sha}], base_ref}`, byte for byte what Bors sends; returns the run title `bors branch=<b> batch=<id> head=<sha> base=<sha>` as the dispatch id |
| `find_candidate_run(repo, *, batch_id, head_sha, base_sha, base_ref="main", since=None) -> CandidateRun \| None` | one list call `GET /actions/workflows/pr-build.yml/runs?event=repository_dispatch&created=<since-60s>..<now+60s>` matched on `display_title`, then the attempt's jobs; `CandidateRun(run_id, attempt, status, conclusion, created_at, started_at, completed_at, runner_name, runner_labels, host, phases)` where `phases` are the jobs and their steps as `(name, started_at, completed_at, status)`; two runs with the same title raise `AmbiguousCandidateRun` |
| `commit_status(repo, sha, context) -> Status \| None` | the latest status for `context` from `GET /commits/{sha}/status`; `Status(state, description, target_url, updated_at, run_id)` with `run_id` parsed from `/actions/runs/<id>` in the target URL |
| `fast_forward(repo, branch, expected_parent, tip) -> FastForwardResult(ok, actual_parent)` | reads `refs/heads/<branch>`; if it is not `expected_parent`, reports the actual parent without writing; else `PATCH /git/refs/heads/<branch>` with `force: false`; a 422 (non-fast-forward) re-reads the ref and reports it as a moved parent |
| `update_ref(repo, ref, sha)` | force-updates `refs/<ref>`, creating it when absent (retained candidate refs, never the base branch) |
| `post_comment(repo, number, body, marker) -> id` | appends `<!-- tauceti-queue <marker> -->`; a comment already carrying the marker is edited (or left alone when identical), never duplicated |
| `set_labels(repo, number, add, remove)` | `POST /issues/{n}/labels` and `DELETE /issues/{n}/labels/{name}` (404 tolerated) |
| `repository_admin(login, repo=None) -> bool` | `GET /collaborators/{login}/permission` is `admin`; `reviewer(login)` is Bors's reviewer check, push access (`admin`, `maintain`, `write`) |
| `rate_limit() -> (remaining, reset_at)` | from the last response headers, else `GET /rate_limit` |

Also used by the server and the command runner: `pr_merge_base`, `collaborator_permission`,
`user_for_token` (the worker endpoints' bearer token resolved through `GET /user`),
`post_commit_status`, `workflow_run`, `close_pr`, `app`, `installation_repositories`.

All calls go through `GitHubClient`, which records the primary limit from every response, waits on
secondary limits (`Retry-After`, or `X-RateLimit-Remaining: 0` until the reset, at most 120 s, at
most four attempts) and retries 5xx with backoff. `client.stats()` (requests, retries, limit waits,
failures, remaining, reset, last error) is part of `/status`.

### Dispatch, polling and verdict binding

A candidate build is the `repository_dispatch` above. `pr-build.yml` derives its `run-name` from the
payload, so the run is found by that title, with the creation window bounded to a minute either side
of the dispatch time; this is the same lookup Bors's `staging_run` performs. The verdict is the
`build` commit status that `finalize-merge-group-build` posts on the candidate head through
TauCetiTools' `finalize_batch.py`, whose `target_url` is exactly
`https://github.com/<repo>/actions/runs/<run_id>`; `Status.run_id` carries that id so the controller
accepts a verdict only when it names the candidate's own run (a status from another run, an earlier
attempt, or a `pull_request` event on a member branch is not the candidate's verdict).

Webhooks come first. `CandidatePoller.poll(refs)` is the fallback: for each pending candidate at most
one run lookup and one status read per `interval` (60 s), and nothing at all while the remaining
quota is below `reserve` (200).

## Commands

The grammar follows bors-ng's `COMMAND_PARSING.md`. A line is a command line when it starts with
`bors` (any case), an optional `:`, and whitespace; each line of a comment is read on its own; runs
of whitespace read as one space; a command word running into a longer word is prose and reads as
nothing (`bors merged this`, `bors cancelled it`, `bors single-handedly`).

| Line | Effect |
| --- | --- |
| `bors r+`, `bors merge` | `controller.approve(repo, pr, head, commenter, None, evidence)` |
| `bors r+ p=N`, `bors merge p=N` | approve with priority `N` (a 32-bit integer) |
| `bors r+ single on\|off` | approve; `single` is accepted and recorded in the evidence but has no effect, the queue has no singleton builds |
| `bors r=login[,login]`, `bors merge=...` optionally `p=N` | approve with the named reviewer; the evidence names the actual commenter |
| `bors p=N` | `controller.set_priority(repo, pr, N)` |
| `bors r-`, `bors merge-` | `controller.unapprove(repo, pr)` |
| `bors cancel` | `controller.cancel(repo, pr)` |
| `bors single on\|off` | accepted without effect |
| `bors ping` | `:pong: pong` |
| `bors r+ sha=<head>`, `bors r+ single sha=<head>`, `bors r- sha=<head>` | the review App's exact-head forms, accepted only from comments whose `performed_via_github_app.id` is the review App (3947238) and only while the live PR head is that SHA and the PR is open; otherwise the comment is ignored as stale, as in Bors's webhook |
| `bors try`, `try-`, `retry`, `delegate`/`d` forms, `link`, `stack`, `unlink` | refused with a reply saying the merge queue does not run them |

Anything after a complete command refuses the whole line with Bors's reply shape (`:-1: bors did not
run \`bors r- now\`: ...`); a modifier that cannot be read (`bors r+ p=abc`) swallows its command; a
line could mean two things (`bors r=alice bob`) is refused, not guessed; a miscased or unspaced
command (`bors R+`, `bors r +`, `bors:r+`, `bors +r`) gets `Did you mean \`bors r+\`?` and runs
nothing. No reply line begins with the trigger.

Permission comes first: a comment needs the level of its most demanding command or reply (`ping`
none; `r-`, `merge-`, `cancel` member; everything else reviewer; unknown refusals fail closed at
reviewer). Bors's reviewer set is the collaborators with push access, so both member and reviewer
resolve to `adapter.reviewer(login)` (`admin`, `maintain` or `write`); the review App is a reviewer
by identity. Someone short of it gets one `:lock: Permission denied` reply. A draft then refuses
every merge-bound command (`r+`, `p=`, `single`, the refusals and corrections of those) with one
reply and runs the rest (`r-`, `cancel`, `ping`).

Evidence recorded with every approval (`commands.Evidence`): the event kind (`issue_comment`,
`pull_request_review`, `review_app`), actor login and id, the comment or review id and URL, its
creation time, the head approved (the live head for a comment, the reviewed commit for a review, the
`sha=` for the review App), the command as typed, `single`, and for repository admins the merge base
of that head against the base branch, which is what Bors's `ManualApproval.grant` records and what
the scope-approval check reads back as `scope_approvals`. Replies are posted with marker
`reply <kind>:<comment id>[:<line>]` so a redelivered webhook edits rather than repeats them.

Not carried over from Bors: delegation and the merge-time delegation gate, `link`/`stack` bundles,
`try` builds, the project-level reviewer list (GitHub's collaborator permission is the source), and
the dashboard link in the permission-denied reply.

## Webhooks

`POST /webhook/github` verifies `X-Hub-Signature-256` (HMAC-SHA256 with the stored secret over the
raw body; 401 otherwise), deduplicates `X-GitHub-Delivery` over the last 4096 deliveries, ignores
repositories the service does not serve, answers `ping`, and for every subscribed event
(`pull_request`, `pull_request_review`, `issue_comment`, `status`, `check_run`, `check_suite`,
`push`, `workflow_run`) calls `controller.handle_event(kind, payload)` after first running the
commands of a created `issue_comment` on a pull request or a submitted `pull_request_review` with a
body. A failure inside the controller answers 500 so GitHub redelivers.

## Feeds

Served at the same paths as Bors, for repository id 1 = `TauCetiProject/TauCeti` (configured in
`ServerConfig.repositories`):

- `GET /repositories/1/active-batches?base=main[&batch_id=N]`: `tauceti-bors.observation/v1`
- `GET /repositories/1/candidates?base=main[&batch_id=N][&promoted=true]`: `tauceti-bors.candidates/v1`

When `base` is absent the service uses the repository's canonical branch, where Bors answered with
`base: null` across all branches; TauCetiTools and the workers always pass `base=main`.

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
| `batch_ids`, `batches[]` | the open batch and the head (`FeedSource.active_batches`), newest first | identical shape; `state` is `waiting` for the open batch and `running` for the head under CI, which the scope-approval check requires |
| `batches[].id/base/sealed/head_sha/base_sha/members[]{pr, head_sha}` | the batch record | identical; the open batch has null `head_sha` and `base_sha`, as Bors's waiting batches do |
| `batches[].promotion_retry_at`, `promotion_failures`, `recovery_after`, `recovery_failures` | the batch's promotion and recovery counters, 0 by default | same ints; the queue's recovery is the repair task, so `recovery_*` stay 0 unless the controller records them |
| `batches[].train` | `{candidate_id, generation, predecessor_id: null, staging_ref}` for a batch with a candidate, null otherwise | identical; `predecessor_id` is always null, there are no speculative trains |
| `held` | always `[]` | Bors held bundle members; the queue has no bundles |
| `bump_repairs` | always `[]` | Bors's bump-repair handoff, read by `tauceti_worker/github.py::bump_repair_requested`; the repair task replaces it, so the worker finds no handoff and leaves the PR to a later round, the same outcome as an empty list today |
| `outcomes[]{pr, head_sha, batch_id, state}` | per open-PR revision, newest batch first, one per `(pr, head_sha)` | identical |
| `scope_approvals[]{pr, actor_id, head_sha, merge_base_sha}` | admin approvals recorded with their merge base | identical; checked by `bors_scope_approval.approved` in the tests |
| `requested_batch` | the `batch_id` batch when it is on `base`, else null | identical to a `batches[]` record |
| `handoff` | a block with Bors's twelve keys: `backend: "queue"`, `pending: 0`, `reason: "queue_service"`, `eligible: []`, `github_count: 0`, `bors_count` the number of active batches, `candidate_scan_complete: true`, `overlap: false`, `updated_at` the service start, `observed_at` now | Bors's merge-backend handoff state; no reader outside Bors consumes it |

### Candidates feed, field by field

| Field | Source in the queue | Compatibility |
| --- | --- | --- |
| `schema`, `repo`, `base`, `timestamp_unit` (`unix_microseconds`), `historical_unknown_reason`, `queue_authority: false` | constants | identical |
| `batch_id` | the query's `batch_id` or null | widened to int or null; the saved responses had no `batch_id` query |
| `candidates[]` (100 most recent) `id/batch_id/generation/observed_at` | the candidate record | identical |
| `candidates[].snapshot{base, base_sha, contract, members, previous_generation_id, speculative_predecessor_id}` | the candidate's members at construction | identical; `contract` is `tauceti-bors.candidate/v1`, `speculative_predecessor_id` always null |
| `snapshot.members[]` | one per member, every Bors key | `pr`, `head_sha`, `assembly_head_sha`, `reviewer`, `original_admitted_at` (microseconds), `manual_approval`, `scheduling.priority` from the records; `patch_id` is the PR number, `admission_id` and `link_id` the admission id (Bors's are its own row ids), `bundle_id` null, `queue_age_retries` and `remainder_retries` 0, `retry_reason` and `parent_*` from the record when a readmission sets them, `scheduling.singleton` the `single` flag, `scheduling.policy` and `label_hint.source`/`binding` descriptive strings, `label_hint.labels` the PR labels at admission with `observed_at` in unix seconds, `source` `queue.admission` |
| `candidates[].events[]{id, kind, source, observed_at, details}` | the controller's journal for the candidate | identical shape; the queue's own kinds (`constructed`, `dispatched`, `status_observed`, `promoted`, ...) use Bors's source names where the meaning is the same (`github.workflow_dispatch_response`, `github.status_webhook`, `github.status_poll`, `github.base_push_response`) |
| `candidates[].runner_assigned_at/started_at/finished_at` | from the run's jobs, microseconds | widened from always-null: Bors never had them |
| `candidates[].unknown_reason` | null once the lifecycle is known, else a string | widened from always-string |
| `admissions[]` (500 most recent) `{id, batch_id, link_id, observed_at, snapshot}` | one per admission | identical; the snapshot has the member keys without `admission_id`, `link_id` and `assembly_head_sha`, as Bors's admission snapshots |
| `promoted=true` | candidates with a `promoted` event, no admissions | as Bors |

## Server

`create_server(controller, config)` returns a `ThreadingHTTPServer` (daemon threads) sharing the
controller object with the controller loop. `GET /health/` is 200; `GET /status` is
`controller.status()` plus `revision` (the checkout's git SHA, or `ServerConfig.revision`),
`started_at`, `github` (client counters) and `webhooks` (received, duplicates, ignored). The worker
endpoints `POST /tasks/claim`, `/tasks/<id>/renew`, `/tasks/<id>/result` and the admin endpoints
`POST /admin/settings`, `/admin/urgent` take JSON bodies and a GitHub token in `Authorization: Bearer`,
resolved to a login through `GET /user` with that token (cached by token digest for five minutes)
and passed to `controller.claim_task(worker_identity, github_login)`, `renew_task(task_id,
worker_identity, github_login, body)`, `submit_task(task_id, worker_identity, github_login, body)`,
`update_settings(github_login, settings)` and `mark_urgent(repo, pr, github_login)`; the worker
identity is `body.worker` or `X-TauCeti-Worker`. A `PermissionError` from the controller is 403,
`LookupError` 404, `ValueError` 400, anything else 500 with the traceback logged; a `None` result is
204.

## Compatibility findings

- The scope-approval check (`bors_scope_approval.verify` for a batch) requires `requested_batch`
  with `state == "running"`, matching `head_sha` and `base_sha`, and 1 to 128 members, and every
  approval to be exactly `{pr, actor_id, head_sha, merge_base_sha}` by an actor whose live permission
  is `admin`. The controller must therefore report the head as `running` from dispatch to verdict
  and record the merge base at admin approval time (done in `commands.run_comment`); a head of more
  than 128 members would fail the check for restricted paths, where Bors capped batches at 128.
- The review App's `bors r+ sha=<head>` comment is produced outside the three repositories inspected
  (TauCetiReview, TauCeti's workflows, TauCetiTools contain no such string); Bors's
  `deploy/cloudflare/README.md` names the App (3947238) and its webhook recognises the comment by
  `performed_via_github_app.id`. The queue does the same, so the producer need not change.
- `tauceti_worker/github.py` reads only `schema`, `repo`, `base` and `bump_repairs` from the
  observation feed; `work_units.py` links to `https://bors.taucetiproject.org/batches/<id>` in a
  prompt, which the queue does not serve (the link is text in an agent prompt, not a read).
- Bors's `repository_admin` matched the actor by user id after resolving the login; the queue checks
  the login's collaborator permission directly, the same endpoint the scope check uses.
- Bors skipped a notice when any comment since the batch carried its `<!-- bors-outbox key -->`
  marker; the queue edits the marked comment instead, so a retried publication converges on the
  final text rather than leaving the first version.
- The `members` payload of the dispatch is `[{pr, head_sha}]`, as Bors's `CandidateJournal`
  snapshot supplies it; `pr-build.yml` forwards it as `BORS_MEMBERS` to the finalizer.

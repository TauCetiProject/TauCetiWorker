# Queue repair task (worker side)

`tauceti_queue/repair_task.py` is the worker-side task of the AI merge queue plan
(`docs/redesign/specs/ai-merge-queue.md`, work plan stream G): a worker claims a repair or
rebase-conflict task from the queue service, prepares warm build artifacts, runs the repair agent in
a private worktree of the content repository, checks the chain it returns, pushes it to the task's
submission ref and submits it. The service alone adopts a chain. The shared machinery (artifact
preparation, prompt, agent run, chain checks) is `tauceti_queue/repair.py`, factored out of the
historical repair experiments so the experiment CLI (`python -m tauceti_queue.repair_experiment`)
and the task use one implementation.

```sh
uv run python -m tauceti_queue.repair_task --service https://queue.example [--once | --check] [--worker ID]
    [--clone PATH] [--root PATH] [--repository owner/repo] [--poll SECONDS]
    [--agent-timeout S] [--build-timeout S] [--keep-worktree] [--seed-packages PATH] [--no-resume]
```

Without `--once` the runner is the long-running worker (`serve`): resume checkpointed tasks, claim,
run, submit, report the provider outcome to `POST /tasks/inference` (a success lifts the service's
inference hold; a release for `inference_unavailable` counts toward it), sleep `--poll` seconds when
nothing is claimable, and log and retry rather than exit when a task, the service or the
authorization check fails. SIGTERM and SIGINT stop a running agent and record the task as
`interrupted` with its claim and worktree kept; the next start resumes the session
(`resume_pending`). It writes a heartbeat to `<root>/worker.json`. `--check` prints the host checks
(tools, token source, repository admin, service, clone, Claude credentials, free disk) and exits.
`--service`, `--worker`, `--clone`, `--poll` and `--seed-packages` default to `TAUCETI_QUEUE_SERVICE`,
`TAUCETI_QUEUE_WORKER`, `TAUCETI_QUEUE_CLONE`, `TAUCETI_QUEUE_POLL_SECONDS` and
`TAUCETI_QUEUE_SEED_PACKAGES`, which is how `services/queue/tauceti-queue-repair.service` configures
it (`docs/queue/operations.md`, "Repair worker"). Otherwise `--clone` defaults to the worker's content
checkout (`Config.resolve().checkout`, the path `agents.prepare_checkout` maintains). `--root` defaults to `$TAUCETI_QUEUE_TASK_ROOT`, else
`$TAUCETI_ACTIVITY_DIR/queue-repair`, else `~/.local/state/tauceti/queue-repair`; it holds the task
checkpoints and logs (`tasks/<task_id>/`), the private worktrees (`worktrees/task-<task_id>`), the
private Lake artifact pool (`lake-cache/`, `lake.toml`) and the agent's isolated Claude config.

## Task protocol

The semantics are the controller's (`tauceti_queue/controller.py`, `docs/queue/controller.md`,
"Task API"): `claim_task(worker_identity, github_login)`, `renew_task(task_id, claim_token)`,
`release_task(task_id, claim_token, reason, error)` and `submit_task(task_id, claim_token, tip_sha,
worker_notes)`, exchanging the `Task` and `Adoption` records of `tauceti_queue/ports.py`. The HTTP
surface is stream F's server (`tauceti_queue/server.py`, `docs/queue/github.md`): `POST` with a JSON
body and `Authorization: Bearer <GitHub token of the worker's account>` (the file named by
`$TAUCETI_QUEUE_TOKEN_FILE`, else `$TAUCETI_QUEUE_TOKEN`, else `$GH_TOKEN`/`$GITHUB_TOKEN`, else
`gh auth token`), the token resolved to a login, a `None`
result as `204`, a bare JSON value as the body otherwise, and errors as `{"error": message}`. The
worker identity is `body.worker` (the runner also sends it as `X-TauCeti-Worker`).

Authorization is the predicate of `tauceti_worker/security_audit.py::require_admin`, applied by the
controller at claim and again at submit; the runner applies the same predicate itself
(`verify_admin`, through `security_audit.api`) before claiming, so a misconfigured worker fails
before it does any work. The check is cached for an hour to spare the account's shared API quota and
repeated for a task whose repository differs from the configured one.

| Call | Body | Response |
| --- | --- | --- |
| `POST /tasks/claim` | `{worker}` | `204` when nothing is claimable; else the `Task` record |
| `POST /tasks/<id>/renew` | `{worker, claim_token}` | the new `expires_at` (a bare number; `{expires_at}` is accepted too) |
| `POST /tasks/<id>/result` | `{worker, claim_token, tip_sha, pushed_ref, worker_notes}` | the `Adoption` record |
| `POST /tasks/<id>/release` | `{worker, claim_token, reason, error}` | `{task_id, released}`; `404` when the service has no such route |

`Task` (`ports.Task.to_dict`): `task_id`, `kind` (`repair` or `rebase_conflict`), `repository`,
`batch_id`, `batch_version`, `base` and `tip` (the chain to start from), `onto` (the commit the
replacement must be based on: the canonical commit for a rebase conflict, `base` itself for a
repair), `members` (`source_pr`, `number`, `accepted_head`, `commit`, `provenance`, `title`,
`description`, `author`, `explanation`, `retained_ref`), `diagnostics`, `retained_refs`,
`submission_ref`, `claim_token`, `expires_at`, `instruction` (the plan's agent instruction, used
verbatim as the prompt's first paragraph), `created_at`, `extra`. The runner renews every
`extra.renew_after` seconds when the service sets it, else every third of the claim's lifetime.

`diagnostics` is the controller's list of findings, each `{kind, module, message}` as the CI
adapter extracted them (`repair.findings_from_diagnostics`): `compiler`/`compiler_error` entries
become compiler errors with the line and column parsed from a `path:line:col` message prefix,
`lint`/`environment_linter` entries lint findings, and every other kind is listed as it is, each with
the file-level owners computed from the chain. A rebase-conflict task carries one
`{kind: "conflict", source_pr, paths, onto, message, pending, clean_prefix_tip}` entry.

`Adoption`: `accepted`, `reason`, `task_id`, `batch_id`, `version`, `tip`, `retained_ref`,
`violations` (chain tool findings, `{code, detail, commit, source_pr}`), `provenance`. The runner
handles the reasons as follows:

| reason | runner |
| --- | --- |
| accepted | outcome `adopted`; the lease stops |
| `explanation-required` | one follow-up turn in the same session asking only for the explanations of the named members, then a resubmission under the same claim |
| `invalid-chain` | one retry (`validation_retries`) of the agent attempt with the violations appended to the prompt, then `not_adopted` |
| `claim-expired`, `claim-superseded`, `unknown-claim`, `not-authorized`, `unknown-task` | the claim is no longer the runner's: outcome `claim_lost`, nothing more is submitted |
| `version-mismatch`, `tip-unavailable`, anything else | outcome `not_adopted` |

A `TaskError` from `renew`, `release` or `result` (`unknown-task`, `unknown-claim`, `claim-expired`,
`claim-superseded`, `not-authorized`) is recognised by its code in the response body (`code`,
`reason` or `error`) at any status, and by a 403/404/409/410 status; the server should answer it as
`409 {"error": detail, "code": code}` rather than through its generic 500 mapping, so that a dead
claim is not retried as a transport failure.

### `release` (new; streams E and F serve it)

`release_task` gives a claimed task back unfinished with a named reason, so the service can hold it or
re-expose it without waiting for the claim to expire; reason `inference_unavailable` counts toward the
controller's inference hold (`report_inference(worker, False, error)`), and `error` is the hold's
"last provider error". Reasons the runner sends:

| reason | meaning for the service |
| --- | --- |
| `artifacts_unavailable` | the worker could not prepare warm artifacts (no published cache mapping for the base, a fetch failure, an incomplete incremental build, or the worker's own cache preparation refusing to start inference); the task was not run cold |
| `inference_unavailable` | every bounded attempt failed with a provider error, a transport failure or exhausted credentials (`agents.classify_agent_failure`); the queue holds the task and re-exposes it (plan, "Inference unavailability") |
| `agent_failed` | the agent ran and exited on its own terms, or returned a chain that fails local validation, twice; the task is re-exposed to any worker |
| `push_failed` | `git-safe-push` to the submission ref failed; the chain is retained locally under `refs/tauceti-queue/task/<id>/attempt-<n>` in the worker's clone |

Stream F's server does not route `/tasks/<id>/release` yet; when the endpoint answers `404` the
runner records `release_unsupported`, stops renewing and lets the claim expire, and the controller
recovers the task the ordinary way. A successful task is not reported through `report_inference`;
adoption lifts the hold.

### Result submission

The proposed tip is pushed to the task's `submission_ref`
(`refs/tauceti-queue/submissions/<task_id>/<claim_id>`, private to the claim) in the content
repository through the worker's own account and `scripts/git-safe-push` (ref compare-and-swap:
create-only for the first push, then against the oid this runner last pushed; a ref left by a
previous incarnation of the same claim is replaced once against its observed oid). `result` then
names the tip; the controller fetches the ref when the tip is not in its clone and retains it under
`refs/tauceti-queue/batches/<batch>/submissions/<sha>` whether or not it adopts it. `worker_notes` is
a JSON string with the worker, the attempt number, the members whose provenance is `edited`, agent
seconds and the estimated inference cost (from the host agent's telemetry and `cost_model`). The
response is recorded in the checkpoint and the outcome.

## Claims and the renewal heartbeat

`ClaimLease` is a thread that renews at `renew_after` until the task completes. A transport failure
or a 5xx does not lose the claim: renewal is retried every `lease_retry_interval` seconds (15 s)
while the claim is unexpired, so the agent keeps its active tool call running through a coordinator
or network outage. The claim is lost when the service refuses the renewal or `expires_at` passes
without a successful one. A lost claim sets the agent's cancellation event (`run_agent_proc` kills
the agent's process group), and the attempt's result is discarded rather than submitted late: the
outcome is `claim_lost`, nothing is pushed or submitted after the loss, and the chain the agent had
reached stays under the local attempt ref. `result` and `release` calls that the service refuses
with a stale-claim code end the task the same way. While a `result` or `release` call is in flight
the heartbeat treats a refused renewal as the service having already accepted the submission (it
frees the claim on adoption and release) and simply ends; the submission's own response decides.

The worker also caps one agent attempt at `agent_timeout` (3 h by default, the experiment's cap).
That cap is the worker's, not the task's: an attempt that exceeds it is an ordinary agent failure
(`agent_failed` after the bounded retry), never a lost claim, and the plan's rule that no repair task
has a time budget holds because the service re-exposes the task.

## Phases

1. **Claim.** `verify_admin`, `claim`, start the lease. Checkpoint `claimed`.
2. **Sources.** Fetch `retained_refs` into `refs/tauceti-queue/task/<id>/retained/...` of the
   worker's clone, check that base, tip and `onto` are commits, retain the input tip under
   `refs/tauceti-queue/task/<id>/input`, and check out the tip detached in
   `<root>/worktrees/task-<id>` (`repair.checkout_worktree`; an existing worktree is re-pointed so
   its `.lake` outputs stay warm). Checkpoint `sources_ready`.
3. **Artifacts.** `repair.prepare_artifacts`, exactly the experiment's recipe: `lake exe cache get`
   for Mathlib (or a shared extracted `.lake/packages` through `--seed-packages`, the experiment's
   stand-in for the shared Mathlib view), `lake cache get --service tauceti-public --repo <repository>
   --rev=<base>` into the private pool, then `lake build --iofail` at the tip, which restores every
   unchanged module and builds the batch's own modules and their dependents. The build is expected to
   fail on the failing modules; artifacts are unavailable only when the base mapping is missing, a
   fetch fails or the build does not complete. Unavailable artifacts release the task
   (`artifacts_unavailable`); the agent never runs cold. Checkpoint `artifacts_ready`.
4. **Agent.** The prompt (`repair.compose_prompt`) opens with the task's instruction verbatim, then
   the batch (where it came from, what it must not do: no push, no PRs, no comments, no `gh`, no
   edits to the build configuration), the diagnostics with file-level owners computed from the chain
   (for a rebase conflict: the conflicting member, its paths and owners, the members not yet
   replayed and the clean prefix), the constituent commits with their PR identities, the recorded
   members with their `Source-PR` and `Accepted-Head` trailers, the editing recipe (fixup commits
   and an autosquash rebase with `--keep-empty --empty=keep`, or any method yielding a linear chain
   on `onto` with one commit per member and every prior message preserved as a prefix; for a rebase
   conflict, `git rebase --onto` with conflicts resolved inside the constituent commits), and local
   verification (`lake build --iofail`, `scripts/lint-env.sh` for lint findings, no weakening). The
   agent runs through `tauceti_worker.agents.run_agent_host` with the claude authoring profile the
   workers use for repair, in the task's worktree, with `repair.isolation_env`: no
   `GH_TOKEN`/`GITHUB_TOKEN`, an empty `GH_CONFIG_DIR`, `remote.origin.pushurl` pointed at a
   non-existent path, an isolated `CLAUDE_CONFIG_DIR` seeded as the workers seed theirs, and the
   private artifact pool's `LAKE_*` variables. The provider session id is written to
   `tasks/<id>/session.json` as the worker's session identity. Checkpoint `agent_running`.
5. **Checks.** `repair.chain_report`, the controller's own checks in its order: `chain.validate`
   against the recorded members on `onto` (identity by `Source-PR`, single-parent history, both
   trailers, count) and `chain.compare` of the input chain `base..tip` against the result on `onto`
   (append-only messages, provenance per member). A chain that validates but changed a member's
   content without an appended explanation gets one short follow-up turn in the same session
   (`--resume`, `repair.explanation_prompt`) asking only for the explanation, as the plan requires of
   the same attempt. A chain that fails validation, a dirty worktree, or a HEAD still at the input
   tip is not acceptable: one bounded retry (`agent_attempts`, 2) with the local report and the
   retained input ref appended to the prompt, then `agent_failed`. Checkpoint `validated`.
6. **Publish and submit.** Push, `result`, record the response (checkpoints `pushed`, `submitted`,
   then `adopted` or `not_adopted`). The worktree is removed afterwards unless `--keep-worktree`;
   the artifact pool is kept, since the next task on the same base reuses it (prune it with the
   experiment CLI's `prune`). Every attempt's tip stays under
   `refs/tauceti-queue/task/<id>/attempt-<n>` in the clone, and `tasks/<id>/outcome.json` holds the
   outcome record.

## Inference unavailability

After an agent exit, `agents.take_last_agent_infra_failure` names the infrastructure reason when the
transcript is nothing but a provider failure: a transient status (408, 429, 5xx, 529), a transport
error, an exhausted credit balance or a subscription limit. Such a failure is retried up to
`inference_attempts` (3) with `inference_delays` (30 s, 120 s) between attempts, continuing the
recorded session when one exists; after the last attempt the task is released with
`inference_unavailable` and the last provider error. An agent that produced a transcript and failed,
or a chain that does not validate, is the agent's own failure and takes the `agent_failed` path; it
is never reported as inference unavailability.

## Recovery

The checkpoint `tasks/<id>/task.json` (written with `runtime_status.atomic_json`, the style of
`repair_recovery.py`'s journal) records the task, the claim token and expiry, the current phase and
a history of phases; `session.json` records the provider session. On start, `resume_pending`
looks for checkpoints in `agent_running`, renews their claim, and when the claim is still the
worker's resumes the same model session in the same worktree with a short continuation prefix on the
original prompt, skipping source and artifact preparation. A claim the service no longer recognises
is recorded as `claim_lost` and not resumed. Bookkeeping loss therefore never restarts a model
session while the claim is live, and a lost claim never produces a late submission.

A task that ends in an error releases its claim (`artifacts_unavailable` before the agent ran,
`agent_failed` after) so the service re-exposes it at once rather than at the claim's expiry. When
the service cannot confirm a release (unreachable, 5xx) the checkpoint keeps it as
`pending_release` beside the claim token (state `release_failed`), and `retry_releases` sends it
again before every claim the loop makes, backing off per `release_backoff`, until the service
confirms it, answers that the claim is gone (`claim_lost`), or has no release endpoint; a checkpoint
left in `error` by an older worker is treated the same. This is what stops a worker from polling
around its own stale claim, which the service refuses for concurrency, for the claim's whole TTL.
Retained refs the service has not published yet (its outbox writes them a moment after creating the
task) are retried every `fetch_retry_delay` seconds for `fetch_retry_seconds` (10 s, 120 s) before
the sources count as unavailable. The refs are fetched 200 refspecs per `git fetch`; when a chunk
fails (git stops at the first ref the remote lacks and names only that one) the failure's exit status
and stderr are logged, the remote is asked once with `ls-remote` which of the chunk's refs exist, the
existing ones are fetched in one call and the rest are reported missing by name; if the listing or
the trimmed fetch fails too the chunk is halved and each half retried, so one bad ref costs log(n)
fetches. Each retry attempt fetches only the refs the earlier attempts did not get.

## Worker loop integration

The runner is importable: `RunnerConfig` (service, clone, root, worker identity, profile and the
bounds above), `make_client`, `AdminCheck`, `claim_and_run(cfg, client, admin)` for one claim and
run, `resume_pending(cfg, client)` for checkpointed tasks. Integrating it as a work unit in
`tauceti_worker/work_units.py` needs:

- a unit that calls `agents.prepare_checkout` for the content checkout, builds a `RunnerConfig`
  with `clone=cfg.checkout`, `root=$TAUCETI_ACTIVITY_DIR/queue-repair` and the round's authoring
  profile, and runs `resume_pending` then `claim_and_run`; `None` from `claim_and_run` means nothing
  was claimable and the unit made no progress;
- the service URL and the token source as worker configuration (`TAUCETI_QUEUE_SERVICE`,
  `TAUCETI_QUEUE_TOKEN`);
- activity accounting from the outcome record (`status`, `agent[*].attempts[*].seconds` and
  `estimated_cost_usd`, `lease.renewals`, `response`), and the `agent-claude-*-telemetry.json` files
  the host agent writes under `tasks/<id>/logs/`;
- the round's own deadline: the task sets `TAUCETI_ROUND_DEADLINE` from `agent_timeout` for the
  agent's shell limits, and a loop that owns the round should pass its remaining time as
  `agent_timeout`.

The service's `repair_concurrency` setting bounds how many workers hold the head's repair task at
once; the runner itself claims one task at a time.

## Tests

`tests/queue_repair_task.py` (registered in `tests/run-all`) runs the task against a fake service
(`http.server` in a thread with the controller's task semantics: claims with expiry within a
concurrency of one, renewals, submissions validated with the chain tool against the bare repository
in the controller's order, `explanation-required`, the `release` call, the `Adoption` shape and
reason codes, encoded as stream F's server does, with scripted responses where a scenario needs
them), a local bare content repository with a three-member attributed chain whose second member
breaks the build, a `lake` stub that fails modules containing `BROKEN`, a `gh` stub for the admin
predicate and a scripted `claude` stub that rewrites the constituent commits. It covers adoption on
the happy path, claim expiry stopping the agent without a submission, renewal errors that do not
stop it, `invalid-chain` retried once with its violations, `explanation-required` resubmitted from
the same session, a terminal rejection, a superseded claim at submission, a locally invalid chain
retried with the local report, the local explanation follow-up turn, artifacts unavailable with and
without a `release` route, inference unavailable after bounded retries, an ordinary agent failure
and the worker's attempt cap released as `agent_failed`, a second claimant and a stale token refused,
resumption of a checkpointed session, and the task record and prompt for a rebase conflict. No real
inference and no network.

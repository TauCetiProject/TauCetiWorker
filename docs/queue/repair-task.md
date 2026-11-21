# Queue repair task (worker side)

`tauceti_queue/repair_task.py` is the worker-side task of the AI merge queue plan
(`docs/redesign/specs/ai-merge-queue.md`, work plan stream G): a worker claims a repair or
rebase-conflict task from the queue service, prepares warm build artifacts, runs the repair agent in
a private worktree of the content repository, checks the chain it returns, pushes it to the task's
submission ref and submits it. The service alone adopts a chain. The shared machinery (artifact
preparation, prompt, agent run, chain checks) is `tauceti_queue/repair.py`, factored out of the
historical repair experiments so the experiment CLI (`python -m tauceti_queue.repair_experiment`)
and the task use one implementation.

Each task member can also carry its accepted PR build's compiled `DECLARATION-NAMES` record.
The service fetches this with its own App in the background (at most two reads in flight), from
the accepted head's newest successful `build` status, the trusted `pr-build.yml` run and its
exact attempt's `sandboxed-build` job. The log must contain one complete, bounded record naming
that head and valid base/tested revisions. Unknown, missing, incomplete and stale records leave
the text-derived context in place; transient reads have three attempts with persisted backoff.
Admission, insertion and CI never wait for a record. The scheduling integration invokes
`Controller._fetch_declaration_names(now)` once per pass and uses its changed result to refresh
predictions.

`.tauceti-repair/decls.jsonl` retains the compiled accepted snapshot and build identity. Clean
members use its exact added names, including generated constants a source scan misses. After an
agent edits a member, its current additions come from the approximate scan, with the original
compiled additions beside them as evidence. Removals, renames and reference ripples remain
explicitly approximate. Cross-batch predictions use the same rules. These are navigation and
admission predictions; the worker's verification audits and the candidate's CI remain authoritative.

```sh
uv run python -m tauceti_queue.repair_task --service https://queue.example [--once | --check] [--worker ID]
    [--clone PATH] [--root PATH] [--repository owner/repo] [--poll SECONDS]
    [--agent-timeout S] [--build-timeout S] [--keep-worktree] [--seed-packages PATH] [--no-resume]
    [--providers claude,codex,api] [--rate T:B[,T:B...]] [--parallel-agents N]
```

Without `--once` the runner is the long-running worker (`serve`): retry pending deliveries, claim,
run, submit, report the provider outcome to `POST /tasks/inference` (a success lifts the service's
inference hold; a release for `inference_unavailable` counts toward it), sleep `--poll` seconds when
nothing is claimable, and log and retry rather than exit when a task, the service or the
authorization check fails. SIGTERM and SIGINT stop a running agent, abandon the attempt and
release the claim as `worker-stopped`. It writes a heartbeat to `<root>/worker.json`. `--check` prints the host checks
(tools, token source, repository admin, service, clone, Claude credentials, build sandbox, free
disk) and exits.
`--service`, `--worker`, `--clone`, `--poll`, `--seed-packages`, `--providers` and `--rate` default to
`TAUCETI_QUEUE_SERVICE`, `TAUCETI_QUEUE_WORKER`, `TAUCETI_QUEUE_CLONE`, `TAUCETI_QUEUE_POLL_SECONDS`,
`TAUCETI_QUEUE_SEED_PACKAGES`, `TAUCETI_QUEUE_PROVIDERS` and `TAUCETI_QUEUE_RATE`, which is how
`services/queue/tauceti-queue-repair.service` configures it (`docs/queue/operations.md`, "Repair
worker"); the `api` provider's `ANTHROPIC_API_KEY` comes from the same environment file (Providers
below). Otherwise `--clone` defaults to the worker's content
checkout (`Config.resolve().checkout`, the path `agents.prepare_checkout` maintains). `--root` defaults to `$TAUCETI_QUEUE_TASK_ROOT`, else
`$TAUCETI_ACTIVITY_DIR/queue-repair`, else `~/.local/state/tauceti/queue-repair`; it holds the task
checkpoints and logs (`tasks/<task_id>/`), the private worktrees (`worktrees/task-<task_id>` while a
task runs, `worktrees/batch-<batch_id>` between tasks), the private Lake artifact pool (`lake-cache/`, `lake.toml`) and the agent's isolated Claude config.

## Task protocol

The semantics are the controller's (`tauceti_queue/controller.py`, `docs/queue/controller.md`,
"Task API"): `claim_task(worker_identity, github_login)`, `renew_task(task_id, claim_token)`,
`release_task(task_id, claim_token, reason, error)` and `submit_task(task_id, claim_token, tip_sha,
worker_notes)`, exchanging the `Task` and `Adoption` records of `tauceti_queue/ports.py`. The HTTP
surface is stream F's server (`tauceti_queue/server.py`, `docs/queue/github.md`): `POST` with a JSON
body and `Authorization: Bearer <GitHub token of the worker's account>` (the file named by
`$TAUCETI_QUEUE_TOKEN_FILE`, else `$TAUCETI_QUEUE_TOKEN`; never `$GH_TOKEN`, `$GITHUB_TOKEN` or
`gh auth token`), the token resolved to a login, or, for a worker on the service's host with
`$TAUCETI_QUEUE_GITHUB_APP`, a `github_app.local_worker_bearer` signed with the queue App's key and
resolved to the App's bot login for the worker endpoints only (operations.md, "Repair worker"), a `None`
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

`Task` (`ports.Task.to_dict`): `task_id`, `kind` (`repair`, `rebase_conflict`, `pre_rebase` or `prepare`),
`repository`, `batch_id`, `batch_version`, `base` and `tip` (the chain to start from), `onto` (the
commit the replacement must be based on: the canonical commit for a rebase conflict, the head's tip
for a pre-rebase of the next open batch, `base` itself for a repair or a preparation, and the head's
tip, like `base`, for a preparation of the pre-rebased chain), `members` (`source_pr`, `number`, `accepted_head`, `commit`, `provenance`, `title`,
`description`, `author`, `explanation`, `retained_ref`), `diagnostics`, `retained_refs`,
`submission_ref`, `claim_token`, `expires_at`, `instruction` (the plan's agent instruction, used
verbatim as the prompt's first paragraph), `created_at`, `extra`. The runner renews every
`extra.renew_after` seconds when the service sets it, else every third of the claim's lifetime. A
`pre_rebase` task is run like a `rebase_conflict` (warm artifacts from `onto` first, run cold rather
than released); its prompt says that the target is the next head's expected base, not `main`, and
lists the `cross_batch` diagnostic's predicted ripples from the head under "Predicted ripples from the
head".

`diagnostics` is the controller's list of findings, each `{kind, module, message}` as the CI
adapter extracted them (`repair.findings_from_diagnostics`): `compiler`/`compiler_error` entries
become compiler errors with the line and column parsed from a `path:line:col` message prefix,
`lint`/`environment_linter` entries lint findings, and every other kind is listed as it is, each with
the file-level owners computed from the chain. A rebase-conflict or pre-rebase task carries one
`{kind: "conflict", source_pr, commit, paths, onto, message, pending, clean_prefix_tip, landed_on_base}`
entry: `commit` is the conflicting commit of the task's chain and `clean_prefix_tip` the commits before
it replayed onto `onto`, published as `refs/tauceti-queue/batches/<id>/conflicts/<prefix>` and named
in `retained_refs`. Such a task resolves that one commit (below, "Rebase conflicts: one commit at a
time"); a worker releases a conflict task without `commit` (made by an older controller) as
`agent_failed`, and the controller replays the head again and replaces the task.

### Rebase conflicts: one commit at a time

A `rebase_conflict` or `pre_rebase` task carries the chain `base..tip` and the target `onto`, and its
`conflict` diagnostic names where the controller's replay stopped (`commit`, `paths`,
`clean_prefix_tip`); the worker does not rely on that SHA and replays the chain itself
(`repair_task.resolve_conflicts`) with the same `chain.replay`, so the whole task runs on one claim
and one worktree and the controller keeps no state between conflicts.

At each conflict the worker checks out the clean prefix and applies the conflicting commit onto it as
the replay computed it (`repair.apply_conflict`: `git merge-tree` with the commit's parent as merge
base, written to the working tree with `git read-tree -u --reset`), so the conflicting files carry
git's markers, HEAD stays at the prefix and no rebase, cherry-pick or merge is in progress. When that
checkout's `lean-toolchain` or `lake-manifest.json` differs from `onto`'s (a member bumps them), the
worker fetches its Mathlib (`lake exe cache get`) before the agent's offline builds need it. The agent
edits files and writes its explanation to `.tauceti-repair/explanation.md`; it gets no rebase recipe,
no step script and no reference to the commits after the conflicting one: its findings are the
conflict and a pre-rebase's predicted ripples from the head into the prefix and that commit, without
the batch-wide admission predictions and preparation errors (`repair_task.conflict_findings`), and
the ownership context is computed for those commits only.

The worker then makes the commit itself (`repair.commit_resolution`): the tree is the worktree's files
as `git add --all` stages them (without `.lake` and `.tauceti-repair`), read through a private index
and the clone's own Git directory; the parent is the clean prefix; the message is the conflicting
commit's own, trailers included, with the explanation appended; the author and author date are that
commit's. The worktree's HEAD, index and refs, and any commit, rebase or reset the agent made, are
never read, so the agent cannot replay or rewrite history. A missing explanation gets one explanation
turn in the same session. These are problems that send the agent round again on the same conflict, up
to `agent_attempts`, with the report as feedback:

- leftover conflict markers in the conflicting files, or a review-size violation of the commit;
- a no-repair or withdrawn member whose resolution changes the prefix (it may only be emptied);
- `absorbed-later-member`: a later member of the input chain, replayed or still pending, that did not
  land on the base, whose change applies onto the clean prefix and changes it, is already in the
  resolved commit, and was not in the conflicting commit itself (`repair_task.absorbed_members`).
  Taking a later member's content is replaying it by hand. A member that conflicts with the prefix is
  not judged, and content moved between members in part is not detected mechanically, as for any
  repair.

**Renamed numeric projections.** When `main` rewrites numeric projections (`x.1` to `x.fst`, as the
toolchain bump to v4.37.0-rc1 did across the library), a member written against the old `main`
conflicts wherever its change touches a rewritten line. Before the agent runs, the worker tries a
mechanical pre-pass (`repair_numproj.prepass`). Take a conflicting commit C with parent P, and a
conflicting Lean file that is a regular file of at most 256 kB in the prefix, P and C, with a numeric
projection in P's or C's version. The worker runs TauCetiTools' numproj tool (`tauceti-tools numproj`,
the installed package's `numproj_fix.py`) on copies of both versions, giving P' and C', and
merges them with `git merge-file` with base P', ours the clean prefix and theirs C'. The tool elaborates
each file against the worktree's warm build, so the pre-pass runs only when the task's artifacts are
available, and the tool runs in the build sandbox like any build of member code, from a fresh copy of
its scripts under `.lake/numproj/run-*` (removed afterwards); the compiled scanner is kept in
`.lake/numproj/cache`, once per toolchain and tree. Elaboration is deterministic and the tool is
idempotent, so a rerun reaches the same result. Its logs are `numproj-<commit>-{parent,commit}.log` in
the task's log directory.

- When every conflicting file merges cleanly and the tool rewrote C's versions completely (exit 0),
  the merge is the resolution: the worker writes an explanation saying so and commits it with
  `commit_resolution`, and it goes through every check below. The outcome's `conflicts` entry has
  `numproj: true` and no agent runs. A resolution that fails a check is discarded and the agent gets
  the conflict exactly as without the pre-pass.
- When a file still conflicts, a file was not rewritten (not Lean, added or deleted, or no numeric
  projection), or the tool left sites in C for the author (exit 1), the agent starts from the merged
  files, clean or with markers between the clean prefix and the rewritten commit, and its prompt says
  which files the worker merged, which still conflict and which sites the tool left.
- Anything unexpected returns the conflict to the agent as before: an installed TauCetiTools without
  numproj, a tool failure or timeout, a sandbox error, a symlink under `.lake/numproj`, a rewritten
  copy that is a symlink or not a regular file, a failed write (the worker applies the conflict again),
  nothing rewritten, or a rewritten copy that is not exactly what the tool writes. The elaborated
  member code can write the tool's copies, so the worker checks each one on the host
  (`repair_numproj.rewrites_only`): it reads off the findings the rewrite implies (numeric projections
  after an identifier or bracket given field names, receivers of identifier characters parenthesized
  just before their `.`, `repair_numproj.projection_edits`), requires each to be in code by the
  installed tool's own lexer (not a comment or string literal), applies them to the original with the
  installed tool's own `apply` (which also rewraps) and requires the identical text. The most the
  sandbox can change is which name a numeric projection gets, which the later build verification
  checks like any resolution.

The controller's insertion probes of waiting PRs do not use the pre-pass: they are `git merge-tree`
checks on the controller host within a twenty-second budget per pass, with no built tree to elaborate
against and no sandbox. A waiting PR that conflicts only over projections is resolved when it reaches a
rebase task, or by its author running `tauceti-tools numproj`.

An accepted resolution is the new base for replaying the rest of the input chain (`chain.replay` from
the conflicting commit onto the resolved one), which stops at the next conflict or reaches the end. A
conflict that stays unresolved releases the task (`agent_failed`). When the replay reaches the end the
worktree is checked out at the whole chain on `onto`, which goes through the ordinary local checks,
verification and full-chain submission, unchanged. The agent never saw the whole chain, so a failing
verification is recorded and the chain submitted without another agent round, and a rejection by the
controller is not sent back to the agent: the head's CI and repair tasks take a chain that does not
build from there. A task with a retained repair checkpoint is already on the target and is repaired as
a chain.

**Resuming after a stop.** Each accepted resolution is recorded in the worker's clone as
`refs/tauceti-queue/batch-resolutions/<batch_id>/<conflicting commit>-<clean prefix tree>`, naming the
resolved commit (`repair_task.store_resolution`). The repair workers share one clone
(`repair-clone`), so whichever worker claims the task next, after a stop (`worker-stopped`), a lost
claim or a release such as `inference_unavailable`, finds them, and the ref keeps the objects. The
key is the prefix's tree, not its SHA: the replay dates its commits when it runs, so a rerun reaches
the same prefix tree under another SHA. At each conflict the worker looks the key up before running the
agent; a stored resolution is committed again on the current prefix as `commit_resolution` would
commit the same files and explanation (`repair.recommit_resolution`: its tree and message, the
conflicting commit's author and date) and goes through every check above. When it passes, the replay
continues from it and the agent is not run for that conflict (the outcome's `conflicts` entry has
`reused: true`). An entry that is missing, names a commit that is not a resolution of this commit on a
prefix with this tree, was not made exactly as the worker makes one (author, committer and dates, so
not a commit an agent made with `git commit`), or fails a check is deleted and the conflict goes to the
agent as before. The agent still sees one commit and never replays. A worker whose claim was lost
records nothing.

The entries belong to the batch, not the task, so a later task of the batch reuses them too: a
pre-rebase superseded at the head's landing, or released, leaves its resolutions for the task that
replaces it onto another tip. The conflicting commit in the key fixes the member's change and its
parent; an entry made on another prefix is reused only when that prefix has the current prefix's
content in every path the commit or the resolution changes (`repair_task.applicable_resolution`), so
the conflict it resolved is this one. Its change is merged onto the current prefix and checked as
above. The lookahead skips a member with such an entry on `onto`. The batch's entries are deleted once
the service has answered a submission of one of its rebase tasks, and when the whole chain fails the
worker's local checks (a rerun would rebuild the same chain without the agent). Entries of a batch
that is never claimed again stay in the clone like its `refs/tauceti-queue/task/<id>/...` refs; they
are a few commit objects.

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
| `artifacts_unavailable` | the worker could not prepare warm artifacts (no published cache mapping for the base or `onto`, a fetch failure, an incomplete incremental build, or the worker's own cache preparation refusing to start inference); the task was not run cold. After such a release `serve` waits `artifacts_backoff` (60 s, doubling to 600 s across consecutive releases) before its next claim, since the service re-exposes the same task at once |
| `inference_unavailable` | every bounded attempt failed with a provider error, a transport failure or exhausted credentials (`agents.classify_agent_failure`); the queue holds the task and re-exposes it (plan, "Inference unavailability") |
| `agent_launch_failed` | the agent process could not be created at all (`OSError` from `Popen`: a missing executable, an argv the kernel rejects); the artifacts were ready, so no `artifacts_backoff` applies, and the error names the launch as the cause. A prompt longer than `agents.PROMPT_ARGV_LIMIT` (64 KiB) is written to the agent's stdin rather than passed in argv, since Linux rejects a single argument above 128 KiB |
| `agent_failed` | the agent ran and exited on its own terms, or returned a chain that fails local validation, twice; the task is re-exposed to any worker |
| `push_failed` | `git-safe-push` to the submission ref failed; the chain is retained locally under `refs/tauceti-queue/task/<id>/attempt-<n>` in the worker's clone |
| `prepared_clean` | a `prepare` task's open batch built on its base without an error, so there was nothing to repair; the controller records the batch version as prepared |

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
   its `.lake` outputs stay warm). When this worker has a parked tree for the task's batch, it is
   moved to `task-<id>` first (`repair_cleanup.adopt`, [One tree per batch](#one-tree-per-batch)).
   Checkpoint `sources_ready`.
3. **Artifacts.** On a warm batch tree (below) nothing is fetched: the tree's own `.lake` is the
   warm state, the record carries `warm_tree`, and any build here or later compiles only what changed.
   Otherwise `repair.prepare_artifacts`, exactly the experiment's recipe: `lake exe cache get`
   for Mathlib (or a shared extracted `.lake/packages` through `--seed-packages`, the experiment's
   stand-in for the shared Mathlib view), `lake cache get --service tauceti-public --repo <repository>
   --rev=<revision>` into the private pool, then `lake build --iofail` at the tip, which restores every
   unchanged module and builds the batch's own modules and their dependents (for a repair with
   streaming on, the default, that build is the worker's background build described after this list,
   and the preparation ends with the fetch). Known audit/lint-only repairs also end preparation
   with the fetch, starting inference before any build or audit discovery pass. The artifacts come from
   the newest revision the cache has: the revision the build runs on (`artifact_revisions` puts it
   first: a repair's `base`, a rebase conflict's `onto`), then its first-parent ancestors walking back
   up to `ARTIFACT_ANCESTORS` (50) commits, each asked with its own `lake cache get --rev=` until one
   answers, then the other requested revision when it is off that walk (a stale `base` further back,
   or the `onto` of a repair whose base is stale). Each fetch's verdict reads only that run's output:
   the per-revision `cache-get-<sha>.log` is appended to across claims of the task, and reading the
   whole file once made a published target look missing because of an earlier claim's miss (production
   repair 2, 2026-11-05). The worker logs `warm artifacts from <sha> (<role>); skipped: <why the newer
   candidates were passed over>`; the record carries `cache_revision`, `cache_distance`,
   `cache_attempts` and `cache_skipped`. The build is expected to fail on the failing modules;
   artifacts are unavailable only when no tried revision has a published mapping, a fetch fails or the
   build does not complete. Unavailable artifacts release a
   repair task (`artifacts_unavailable`; its agent never runs cold), while a rebase conflict, whose
   rebase needs no build, runs its agent without warm artifacts and leaves the validation build to run
   cold or to the service's CI. When the workers' repair namespace is configured, the fetches start
   there (`lake cache get --service tauceti-repair`) for the task's `tip`, `onto` and `base` before
   any public revision; `cache_service` records which service the restored mapping came from.
   Checkpoint `artifacts_ready`.

   **Warm revisions.** A `pre_rebase` task builds on a queue head's tip, which is never on `main` and
   so never in the public cache, and a `prepare` task builds a batch whose own tips the workers may
   already have published. The controller therefore puts `warm_revs` on every `pre_rebase` and
   `prepare` task record, computed at claim time (`Controller._warm_revs`): for a `pre_rebase`, the
   head's current tip and its previous tips (newest first, up to `REPAIR_OUTPUTS_PREVIOUS_TIPS`, from
   `batch_tips`), then the head's base; for a `prepare` of the pre-rebased chain, the head's tips, then
   the batch's own tips (the one the pre-rebase covers first); for a `prepare` on its own base, the
   batch's previous tips, then that base. Every kind also lists the tips of the last two landed heads
   (`Controller._landed_tips`, see controller.md) after those batch tips and before the base. Before the agent runs, the worker fetches the repair namespace's outputs for every one
   of them, then for the task's `tip`, `onto` and `base` (`repair_publish.warm`, without repeats). All
   are tried, not only the first that answers: Lake keys outputs by input trace, so each mapping adds
   the artifacts of its own tree and a nearly identical tree's outputs serve nearly completely. A
   revision without a mapping is skipped. For a `pre_rebase` the public walk then starts at the last warm
   revision (the head's base, a `main` commit) rather than at the head's tip. The record carries `warm_attempts`
   and `warmed_from`, and the worker logs `repair outputs warmed the tree from <shas>; no published
   mapping for <shas>`. A warm hit alone makes the artifacts available. The prompt for every task kind
   (`repair.CACHE_SENTENCE`) says the worker has already placed every artifact it could find, that the
   agent must not run `lake exe cache get` or any other cache fetch, that a public-cache miss for a
   queue head's tip is expected and is no reason to stop, and that it proceeds to build
   incrementally. The queue launches agents with `run_agent_host(..., cache_note=False)`, so the
   fleet's build-cache appendix (its fetch commands and the stop rule for `TAUCETI_PR_CACHE_MISS`) and
   that variable never reach a queue agent; the fleet's own rounds keep both. Production batch 3's
   `pre_rebase` task (2026-11-05) is the case: Codex fetched the public cache itself, found nothing for
   head 2's tip, followed the appendix's stop rule and aborted the replay twice.

   **Sandboxed builds.** Every `lake build` the worker runs goes through
   `repair_sandbox.sandboxed_lake(argv, worktree, env)` (`tauceti_queue/repair_sandbox.py`), which wraps
   it in the offline `bwrap` sandbox CI's required build uses (TauCeti `pr-build.yml`, "Build exact
   candidate under bwrap", running TauCetiTools `sandbox-build.sh`): `env -i bwrap` from a read-only
   synthetic root with the host's system directories bound read-only, `/tmp` a tmpfs, no home
   directory except the elan toolchains (read-only), the worktree read-only with only its `.lake`
   writable, CI's namespace flags including `--unshare-net`, and `LAKE_NO_CACHE=true`. The worker's
   own additions are its artifact pool (`LAKE_CACHE_DIR`, writable, since Lake stores every built
   artifact there), `LAKE_CONFIG` and a shared `--seed-packages` tree (read-only), and the content
   clone's Git directory (read-only, so Lake's Git queries resolve). The arguments, `--iofail`
   included, and the exit status pass through unchanged. Only the build is sandboxed: the cache
   fetches before it need the network, and the agent's own shell is not confined.
   The agent's scoped builds through `.tauceti-repair/build` import the same function from the
   worker's checkout, and `python -m tauceti_queue.repair_sandbox [--worktree DIR] -- lake build ...`
   is the same wrapper on the command line. Because the pool and the worktree's `.lake` are separate mounts inside the
   sandbox, Lake copies artifacts between them instead of hard-linking (its own fallback), so a
   worktree's `.lake/build` takes its full size on disk until the worktree is removed. When `bwrap`
   is missing the build fails closed; `TAUCETI_QUEUE_SANDBOX=0` runs builds unsandboxed, logging a
   warning for every build and reporting the sandbox as DISABLED in `--check`.
4. **Agent.** The prompt (`repair.compose_prompt`) opens with the task's instruction verbatim, then
   says what happened in plain words (for a rebase conflict: the queue is replaying this batch onto the
   current `main` commit with git, the replay stopped at PR N's commit with conflicts in these files,
   the commits before it replayed cleanly and are at this commit, which is HEAD, and the job is to
   resolve that one commit; its instruction is `repair.RESOLUTION_INSTRUCTION` rather than the
   service's; for a repair: the batch was built on `main` with CI and the build failed), the evidence
   with the git refs the agent needs named once (base, target, the conflicting commit, the clean
   prefix, and for every file the owning commit: "PR N's commit `sha` modifies this file, and `main`
   has changed it since the batch's base"; for a repair the compiler, audit and lint findings with
   their owning commits), the result the queue expects (a linear chain on `onto` with one commit per
   PR in the same order, each keeping its `Source-PR:` and `Accepted-Head:` trailers and its ` (#N)`
   subject suffix; the queue reads it from the worktree, validates it and runs CI itself, so there is
   nothing to push or report and `gh` is not needed; no changes introduced to the build configuration,
   `scripts/` or `.github/`, though a constituent commit that modifies such files keeps its change;
   for a rebase conflict, the files in the worktree, which the worker commits itself), one editing
   instruction (for a rebase conflict: remove the markers so the files hold the commit's change on
   HEAD, then write the explanation file; for a repair: explanations appended with
   `git commit --allow-empty --squash=<sha> -m <explanation>` and content changes with
   `git commit --fixup=<sha>`, folded in by an autosquash rebase that keeps each commit's message and
   author), and local verification. The member listings are not in the prompt (1,197 members once
   made a 291 KB prompt, about 75k tokens of noise): `repair.write_listings` writes
   `.tauceti-repair/members.jsonl` in the worktree, one JSON object per member in chain order with
   `position`, `commit`, `pr`, `title`, `accepted_head`, `provenance`, `landed_on_main` (the landing
   commit, else null) and, for a rebase conflict, `state` (`replayed` or `conflicting`: its listings
   hold the clean prefix and the conflicting commit, never the commits after it), and beside it
   (except for a rebase conflict) `.tauceti-repair/check`, the agent's one validation command: a standalone Python
   script that verifies the chain is linear on the target, has exactly one commit per member in the
   recorded order with the trailers and ` (#N)` subjects kept and every original message preserved as
   a prefix, that no rebase is in progress and that the working tree is clean, then runs staging's
   member review-size policy on each member's commit against its parent (the shared
   `ci_git.pr_guards`, copied beside it as `.tauceti-repair/ci_git.py` from the worker's TauCetiTools),
   and prints the first problem or `ok`; `check --size <commit>` prints one commit's measured sizes
   and violations as JSON. When the task's findings include a `policy` finding, the prompt explains
   the policy and its caps and says that splitting the member's change inside the batch (for example
   a too-long new file into smaller files, in that member's own commit) is a valid repair. The
   directory is excluded from Git through the clone's `info/exclude`, so no recipe the agent runs can
   commit it, and `checkout_worktree` leaves it alone; a prompt composed without a worktree (the
   experiment's preview) carries the listings inline. The verification advice follows
   the artifacts outcome: a warm rebase fetches cached outputs without compiling the old chain and
   asks the agent to resolve the conflict before building the result. Warm repair/prepare tasks
   with worker verification enabled instead use `.tauceti-repair/check-findings`: scoped compilation
   of the reported modules and actual repair delta, then the named linters or ownership-only check.
   They return for the worker's full verification; their build helper requires module arguments.
   Other configurations retain the incremental whole-library advice. When a
   rebase conflict runs without them (`cold`) it says so, gives the reason, warns that
   `lake build --iofail` starts from nothing and may take hours, and asks for the touched modules
   (`lake build <Module>`) to be built first. Either way the prompt tells the agent to check a fix
   with the scoped build first; when the worker owns full verification it is the worker's next
   step after the agent returns. Otherwise the agent runs `lake build --iofail` as its final verdict.
   `.tauceti-repair/build` (written by `repair_tools.write_build_script` beside the check
   script, a Python script of under eighty-five lines) takes module names (`TauCeti.Foo.Bar`) or paths
   (`TauCeti/Foo/Bar.lean`), with `--dependents` every module importing them transitively (from the
   library's `import` lines, read in one pass and cached in `.tauceti-repair/imports.json`, which the
   worker drops whenever it rewrites the script), and runs `lake build --iofail` on them in the
   foreground, exiting with Lake's status; with no arguments it builds the whole library and, when that
   is green, runs CI's audits and full-library lint (below; `--no-audit` skips them, a scoped build
   prints that they run on the whole-library build). Every build
   in the worktree holds the lock directory `.tauceti-repair/build.lock`, taken atomically by `mkdir`
   with the holder's pid in `pid` inside: a waiter polls every 2 s, prints a line every 30 s, and
   clears a lock whose holder has died. The worker's own builds hold the same lock through
   `repair_tools.build_lock`, so exactly one `lake build` runs in a worktree at a time whoever
   started it. Long builds run in the foreground: the task
   sets `TAUCETI_SHELL_LIMIT_SECONDS` to `RunnerConfig.shell_limit` (two hours;
   `repair.SHELL_LIMIT_SECONDS`) for the agent's shell limit, where the fleet's authoring rounds
   default to an hour per command, and the prompt says so. The prompt states the aim (every constituent PR's mathematics
   lands as its author intended, compiled and checked) before the hard rules (no `sorry`, no
   `native_decide`, no new axioms, no `set_option`, no silenced linters), and asks for a closing
   summary of what changed in which PR's commit and the exit status of the final build. The
   agent runs through `tauceti_worker.agents.run_agent_host` with the claude authoring profile the
   workers use for repair, in the task's worktree, with `repair.isolation_env`: no
   `GH_TOKEN`/`GITHUB_TOKEN`, an empty `GH_CONFIG_DIR`, `remote.origin.pushurl` pointed at a
   non-existent path, an isolated `CLAUDE_CONFIG_DIR` seeded as the workers seed theirs, and the
   private artifact pool's `LAKE_*` variables. The provider session id is written to
   `tasks/<id>/session.json` as the worker's session identity. Checkpoint `agent_running`.

   Before the first attempt the worker also gives the agent ownership and declaration context
   (`repair_context.compute`, called once the worktree exists; the result goes on
   `PromptContext.repair_context` and its summary into the outcome record): for every member, the
   files its squash commit changes (`.tauceti-repair/files.jsonl`: `position`, `pr`, `commit`,
   `files`) and the declaration names it adds, removes or renames (`.tauceti-repair/decls.jsonl`:
   `added`, `removed`, `renamed` pairs, marked `approximate: true` because they come from a line scan
   of each changed Lean file's two versions for `theorem`, `lemma`, `def`, `abbrev`, `instance`,
   `structure`, `class`, `inductive`, `axiom` and `alias` under their `namespace`/`section` nesting,
   not from the compiled environment; a removal and an addition in one file sharing a dotted or
   underscored prefix or suffix are paired as a rename), and from those the collisions (a name added
   by two or more members) and rename ripples (a name removed or renamed by one member and still
   referenced, whole word, in another member's changed files at the chain tip or in the target's
   version of a file no member changed). Every collision and ripple is in
   `.tauceti-repair/collisions.jsonl` with line numbers; the prompt's "Likely collisions and
   renames" section lists them with member positions, PRs and files up
   to `repair_context.SECTION_LINES` bullets and points at that file for the rest. One `git
   diff-tree` per member and `git cat-file --batch` for the changed blobs run over a thread pool, so
   a 1,197-member head takes about a second; a failure in this step is recorded in the outcome and
   the prompt simply lacks the section.
5. **Checks.** `repair.chain_report`, the controller's own checks in its order: `chain.validate`
   against the recorded members on `onto` (identity by `Source-PR`, single-parent history, both
   trailers, count) and `chain.compare` of the input chain `base..tip` against the result on `onto`
   (append-only messages, provenance per member), and then staging's member review-size policy on
   each non-empty commit of the result against its parent (`repair.review_size_violations`, the same
   `ci_git.pr_guards` as admission and staging), each rejection a problem. A chain that validates but changed a member's
   content without an appended explanation gets one short follow-up turn in the same session
   (`--resume`, `repair.explanation_prompt`) asking only for the explanation, as the plan requires of
   the same attempt. A chain that fails validation, a dirty worktree, or a HEAD still at the input
   tip is not acceptable: one bounded retry (`agent_attempts`, 2) with the local report and the
   retained input ref appended to the prompt, then `agent_failed`. Checkpoint `validated`.
   **CI's audits on the worker** (`tauceti_queue/repair_audit.py`; setting `TAUCETI_QUEUE_WORKER_AUDITS`,
   default on, and `--no-worker-audits`). Head 2's first CI round after a green worker build failed in the
   audit phase on a duplicate declaration the agent's text scan had listed and set aside, so the checks are
   the worker's, not the agent's. After every green whole-library `lake build --iofail` it runs (the
   streaming build, the parallel path's verification build, the verification below and, in configurations
   where the worker does not own final verification, the agent's full-library helper) it runs every check
   staging runs, all at once: the `audit-driver` under `lake env timeout 3000s` (import of every TauCeti
   module, duplicate declarations, axioms), the module-system check (`ModuleSystem.lean`), `lint-env.sh`
   with the `lint-env-driver`, `lint-style.sh` (Mathlib's text linters and the
   header check) and `lint-dot-notation.py` against its baseline. Every task, head or background, gets
   the same set; there is no check owner and nothing is deferred to staging. The environment lint
   first covers only the TauCeti modules changed since the base (`LINT_ONLY_MODULES` with
   `LINT_CHANGED_ONLY=1`, an ordinary PR's preflight scope), whose time follows the changed
   declarations rather than the whole library's 20 minutes. When that round finds nothing in any
   check, the whole-library lint runs before the checks pass, so `passed` always includes it, and
   `lint.scope` is `changed` only in a
   failed round (`changed_lint` records the changed-module run of a passing one). The whole-library
   lint takes the changed-only run's place in two cases, recorded as `changed_lint.skipped`: more than
   `CHANGED_LINT_MODULE_LIMIT` (1,500) changed modules (`module_limit`), since `lint-env.sh` writes the
   selected modules into one array literal that Lean's code generator rejects a little under 3,000
   elements, and a changed-only run that ends in a `lint-env.sh` driver failure (`LINT-ENV: FAIL` naming `driver failure`) with no finding
   naming a module (`driver_failure`), whatever the other checks found. A finding outside
   the changed modules, such as a new simp lemma breaking an unchanged declaration's simp normal
   form, therefore surfaces in that final lint and costs another round. The sources are the TauCetiTools pin
   the worktree's `tauceti.toml` names (`[tools.tools] repo`, `ref`): fetched once into
   `<root>/tauceti-tools/<ref>`, materialized with `python -m tauceti_tools materialize` and the worktree's
   `scripts/` as policy directory into `.lake/tauceti-repair-audit/scripts`, with both drivers compiled by the
   pinned `build-lint-driver.sh` and `build-audit-driver.sh` inside the sandbox (reused while pin, toolchain
   and manifest are unchanged); the checks run in the sandbox too. Their output is read by
   `diagnostics.parse_log`, so each finding is the diagnostic the controller would create from CI
   (`{"kind": "audit", "module", "path", "message": "duplicate-declarations: ..."}` once per TauCeti module,
   `lint` with its location; the dot-notation and style lints' compiler-shaped lines are `lint` too), and it
   is appended to `.tauceti-repair/errors.jsonl` beside the compiler errors.
   `repair.findings_from_diagnostics` turns a duplicate into one entry naming both modules, and the parallel
   path keeps both owners in one group. When the checks cannot run at all (no pin, a failed fetch,
   materialization or driver build) they are recorded as `unavailable` and logged, and CI remains the only
   check. For a repair with warm artifacts, after the chain checks pass the worker runs its own verification
   (`repair_audit.verify`, under the build lock: the whole-library build, then the checks); a failure is not
   submitted: the agent is run again with the findings as the task's diagnostics and the report as feedback,
   up to `verify_retries` (2) times, then the task is released as `agent_failed`. Each verification is in the
   outcome's `verification` list. For warm repair/prepare/rebase tasks the prompt gives the agent focused
   feedback checks and assigns the complete verification to the worker, avoiding a duplicate agent
   pass. The task cannot be submitted while the worker's checks report failures, and one round's findings
   from every check reach the agent together. In CI runs of 2026-11-05 on the full library (24,455 TauCeti
   modules) the audit's import took 5 s warm and 27 s cold, the duplicate check 3 s and the axiom scan of a
   PR's few modules 4 s; the lint of five changed modules took 45 s and the two driver builds about 4 s.
6. **Submit and publish.** Push, `result`, record the response (checkpoints `pushed`, `submitted`,
   then `adopted` or `not_adopted`), then publish the build outputs (below). The worktree is then
   parked as the batch's tree (by the background publisher when one runs) unless `--keep-worktree`;
   the [lifecycle collector](operations.md#terminal-repair-worktree-cleanup) removes it once the task
   or batch is terminal, including trees left by errors or process kills;
   the artifact pool is kept, since the next task on the same base reuses it (prune it with the
   experiment CLI's `prune`). Every attempt's tip stays under
   `refs/tauceti-queue/task/<id>/attempt-<n>` in the clone, and `tasks/<id>/outcome.json` holds the
   outcome record.

### One tree per batch

A worker keeps one tree per batch, with its `.lake`, across the batch's tasks and across worker
restarts (`tauceti_queue/repair_cleanup.py`). When a task ends, or a restarted worker abandons it,
its tree is renamed to `<root>/worktrees/batch-<batch_id>` (`retire`, recording the task in
`.lake/tauceti-batch-tree.json`, written before the rename so it moves with the tree). Only a tree whose artifacts were prepared (`available`), and whose
`lean-toolchain` and `lake-manifest.json` are still the prepared ones (`onto`'s for a rebase, the
starting tip's otherwise), is parked (`repair_task.parkable`); the tree is locked in Git while it is
renamed, so a concurrent `git worktree prune` on the shared clone keeps it; any other is removed as before, and so is a tree whose batch already has a parked
one. The batch's next task on this worker renames the parked tree to its own `task-<id>` path
(`adopt`). Each worker has its own root, and a rename takes the tree whole and fails when its
destination exists, so two tasks never share or nest one. Adoption, parking and the collector take
the tree's lease (`worktree-leases/batch-<id>.lock`), so the collector never deletes a tree a task
is taking. The background publisher parks the tree only when the task's artifacts were available. Adoption deletes the previous task's `.tauceti-repair/` (which `git clean` keeps, since the clone
excludes it), `.lake/outputs.jsonl` and `.lake/publication-stage`, and quits any Git operation left in progress;
the checkout then re-points the tree to the task's starting tip as for any reused tree.

The adopted tree is warm when `lean-toolchain` and `lake-manifest.json` are unchanged between the
commit it was parked at and every revision the task builds (its starting tip, and `onto` for a
rebase). A warm tree fetches nothing: no Mathlib cache, no repair or public outputs, no ancestor
walk. A miss means Lake compiles that module; it never releases the task. A tree whose toolchain or
dependencies changed, or a batch's first task on this worker, prepares artifacts as described above.
`outcome.sources.batch_tree` is `warm`, `adopted` (re-prepared) or `new`.

The queue's agent rounds run with `host_caches=False` (`agents.run_agent_host`): the host's shared
cache preparation and the canonical store's prepare and round-end relinking belong to the fleet's
rounds, and the task has already prepared its own tree. A parked tree therefore holds its whole
`.lake` (about 23 GiB of build outputs and 7 GiB of packages for TauCeti in November 2026). The
lifecycle collector removes it once its batch has landed or been removed. Parked trees do not hold
back the artifact pool's collection (`repair_cache`): Lake promotes a collected output back into the
pool from the tree when it next needs it.

**Known audit/lint findings.** When a repair task already carries located audit or lint
findings for its frozen candidate, and no compiler errors, the worker fetches the warm artifacts
and starts the agent directly. It skips both the sequential preparation build and streaming
build/audit discovery, even under `--no-stream-repair`. The task's findings and ownership context
are supplied in the prompt. This avoids waiting for another whole-library lint pass to rediscover
the failures that created the task. Final build and audit verification still runs before submission;
remaining failures go back to the agent or prevent submission. Predictions, unlocated messages,
generic failed statuses, compiler failures, preparations and rebases keep their existing flows.
The outcome records this choice as `pre_repair_discovery`.

**Repair while the build still streams** (`tauceti_queue/repair_stream.py`; setting
`TAUCETI_QUEUE_STREAM_REPAIR`, default on, and `--no-stream-repair`). The first production repair spent
its whole first `lake build --iofail` pass collecting errors before touching anything, so for a repair
task with warm artifacts the worker runs that build itself: once the worktree and the cache fetch are
ready it starts `lake build --iofail` in the worktree in the background with its log at
`.tauceti-repair/build.log`, holds `.tauceti-repair/build.lock` while it runs, tails the log and
appends each compiler error (`file:line:col: error: ...` with its continuation lines) to
`.tauceti-repair/errors.jsonl` as it appears, as one JSON object with `module`, `file`, `line`,
`column`, `message`, `member` (the PR whose squash commit last touched the file) and `commit`. The agent
is launched at the first error, or when the build ends, whichever comes first, with a "Build in
progress" section before the verification advice: the build is running in the background, its log and
error list are at those paths and grow, modules that have already failed may be edited now, no `lake
build` of its own may start while the lock exists, and `.tauceti-repair/build <Module> ...` (the scoped
build script of `tauceti_queue/repair_tools.py`, whose lock the streaming build takes through
`take_lock` so the two never overlap) waits for the lock and builds only the named modules.
Lake from v4.37.0-rc1 fails a module whose source changed while Lean compiled it ("module source
file: ... changed since build began; rerun the build", https://github.com/leanprover/lean4/pull/16457),
which here means the agent edited it under the running build. The worker records that as a `kind`
`rerun` entry (`module`, `file`, `message`, no member) rather than a compiler error, counts it in the
`build finished` line and the outcome's `stream_build.reruns`, leaves it out of the parallel path's
findings, and the prompt tells the agent to rebuild the module and its dependents with the scoped build.
When the build ends the worker appends a `build finished, exit N, M errors total` line to the error
list, removes the lock and copies the log to `tasks/<id>/logs/stream-build.log`; the outcome records
the build under `stream_build`. When worker verification is enabled, the agent returns after focused
checks and the worker runs the final full verification. Other configurations retain the agent's
whole-library `lake build --iofail` verdict. A build still running when the task ends or is interrupted is stopped and
its lock removed. Rebase-conflict and pre-rebase tasks fetch caches and go directly to the agent,
which resolves the one conflicting commit before building the result. For repairs without known audit/lint findings, `--no-stream-repair` keeps the sequential
flow: the preparation's own incremental build, then the agent.

**Preparing an open batch** (`kind: "prepare"`; `docs/queue/controller.md`, "Background
preparation"). While the head waits on CI or is repaired the controller hands out `prepare` tasks for quiet open
batches: `base` and `tip` are the open batch's own (never the head's tip), `onto` is `base`, and
`diagnostics` is empty because no CI run has seen the batch. The worker runs it as a repair with the
streaming build, always, even under `--no-stream-repair`, since that build is how it learns whether
anything fails: a build that finishes with exit 0 and no error releases the task as `prepared_clean`
without starting an agent and publishes its outputs to the repair cache under the batch tip. When
the build reports errors the agent is launched as for a repair, with
the "What happened" section saying that the batch has not yet been tried, that it should fix what
fails to build so the batch is ready when it becomes the head, and that nothing it does is a verdict;
the result is validated, pushed and submitted the same way, and the controller adopts it into the
open batch's members, which carry the repairs through the batch's later rebase as the head. The
claim's `extra.renew_after` is 60 seconds: the controller supersedes a preparation when its batch
seals or changes, and the refused renewal stops the agent within a minute. The submission's notes
carry `remaining_errors` (`prepare_remaining_errors`): none when the parallel path's verification
build passed, otherwise the compiler errors handed to the last agent (the streaming build's
`errors.jsonl` merged with the diagnostics), which no worker build showed fixed. The controller
passes them to the batch's later `pre_rebase` task as a `prepare_errors` diagnostic, which the
prompt lists under "Errors the batch's preparation left".

**Preparing the pre-rebased chain** (a `prepare` task whose `extra.pre_rebased` is set;
`docs/queue/controller.md`, "Preparing the pre-rebased chain"). After an open batch's pre-rebase onto
the head's tip, the controller hands out a second preparation of the replayed chain: `base` and
`onto` are the head's tip, `tip` the pre-rebased tip (fetched from
`refs/tauceti-queue/batches/<id>/pre-rebased/<tip>`), the members are the commits of that chain, and
`extra.pre_rebased` names the batch tip it covers (`source`), the batch's own base (`batch_base`) and
the head's base (`head_base`). The worker runs it exactly like an own-base preparation, so the
streaming build, the in-place repair with edits folded into the owning members'
commits, the worker's audits (against the head's tip) and the submission are unchanged. Only the
artifacts differ: the repair namespace is asked for the pre-rebased tip and then the head's tip
(`repair_publish.preferred`), the outputs the head's repair published, and the public cache for the
head's tip, its first-parent ancestors, then `head_base` and `batch_base` (`artifact_revisions`),
since the public cache never has the head's tip; the log names the revision restored as the head's
tip, the head's base or the batch's own base. The prompt's "What happened" says that the batch has
been replayed onto the head's tip, the commit it becomes the next head on, and that the head's own
commits below it are not the agent's to edit. A green build releases the task as `prepared_clean`
("the batch built on the head's tip without errors"), otherwise the result is submitted, and in both
cases the publisher then publishes the outputs for the tip: the pre-rebased tip, or the submitted
one. The controller adopts that tip as the batch's pre-rebase, so a head that lands unchanged seals
with it and CI's fetch for the exact staging head finds these outputs.

## Repair cache publication

The final build writes `.lake/outputs.jsonl` with `--rehash -o` when publication is configured.
`lake cache stage` copies that native map and its complete artifacts; stock `lake cache put-staged`
uploads them with an explicit repository, revision and toolchain from an isolated staging directory,
without loading candidate workspace configuration. There is no second build scan or reconnect pass.
The upload has a bounded timeout (`TAUCETI_REPAIR_CACHE_PUBLISH_TIMEOUT`, default 1800 s),
and the outcome's `publish` record holds artifact count, bytes and seconds. A failure or a skip
is logged and recorded and never fails the task. The namespace is two services the worker writes
into the pool's `lake.toml`: `tauceti-repair` reads from `TAUCETI_REPAIR_CACHE_URL` (public, since Lake
fetches without credentials) and `tauceti-repair-upload` writes to `TAUCETI_REPAIR_CACHE_UPLOAD_URL`
(the S3 endpoint of the same bucket; default the read URL), each holding `artifacts/` and `revisions/`
laid out like the public cache. The write key, the `access-key-id:secret` pair Lake takes as
`LAKE_CACHE_KEY`, is read from the file named by `TAUCETI_REPAIR_CACHE_KEY_FILE` and given only to the
`lake cache put-staged` process; `LAKE_CACHE_KEY` is removed from the environment the agent inherits. Use a
bucket of its own with an R2 token scoped to it, so the worker's key cannot write the public cache
CI publishes to (`LAKE_CACHE_KEY` in TauCeti's `publish-lake-cache.yml`). `TAUCETI_QUEUE_PUBLISH_CACHE`
(`1`/`0`) switches publication on or off; unset, it is on when the key file and an upload URL are
configured. CI fetches from this namespace only the exact staging head (TauCetiTools
`repair-cache-get.sh`), and Lake still checks every trace against the candidate's inputs, so its
verdicts are unchanged.

Publication never delays the submission. On 2026-11-05 head 2's repair validated at 09:18:58 UTC and
the publish step (the `--no-build --rehash` pass over about 26,000 modules, then the upload) ran for
more than 12 minutes before the chain was submitted, and CI, dispatched only after submission, waited
with the whole queue. `TAUCETI_REPAIR_PUBLISH` (`after`, the default; `before`; `off`) now chooses
the order. With `after`, a green full-library build now first freezes an output map and
complete native artifacts and starts a background upload while audits and lint run. Stock Lake's
`cache put-staged --rev=...` uploads for that captured revision without consulting the live
workspace. Compilation outputs are reusable even if audits subsequently reject the chain;
checks and submission remain independent. At submission an upload for the exact accepted
revision is reused without waiting. A changed revision, failure or unavailable snapshot
retains the late-publication fallback described below.

Under that fallback the worker pushes and submits exactly as before (the same checkpoint token,
the claim released or adopted by the submission) and then starts `repair_publish.start_background`
for the submitted tip, after `adopted` or `not_adopted` and after a `prepared_clean` release, and
goes on to its next claim. The publisher is a process of its own: under systemd a transient unit
(`systemd-run --user`, since the worker unit's `KillMode=mixed` kills whatever is left in its cgroup
on a stop or restart), elsewhere a new session, so a stopping worker does not take it down. It writes
`tasks/<id>/logs/publish.log` and its record `tasks/<id>/logs/publish.json` (`published`, `skipped`
or `failed`), removes the task's worktree when done, and `worker.json` reports running publications,
counts and the last result and failure under `publish`; a failure is logged there and in the journal
and never changes the task's outcome. Native upload runs under `nice -n 10`, so
a concurrent repair or build on the host keeps the CPU. `lake cache put-staged` uploads every artifact
before the revision mapping the fetcher reads, and exits without writing the mapping when an
artifact upload fails, so CI never sees a mapping for a partially uploaded set; a publisher killed
mid-upload leaves no mapping, which the fetch script treats as the normal miss. `before` keeps the
first ordering (publish in the task, before the push) and `off` publishes nothing; the read side is
unchanged in all three.

## Parallel agents within one task

Before the agent of phase 4 runs, a repair task's findings are split into independent groups
(`tauceti_queue/repair_parallel.py`, `plan_groups`). The findings are the ones the task already has
(its diagnostics); the path never waits for a streaming build, so the agents start at once. A
preparation, which has no diagnostics, therefore repairs sequentially from the streaming build's error
list. Two failing modules share a group when one
member's commit changes both, when the owning members' commits change a common file, or when a file
one member changes `import`s a file of the other's (read from the files at the tip; nothing deeper is
inferred). A finding in a module no member changes (a ripple into `main`) cannot be assigned and keeps
the task sequential, as does a single owning member or a provider chain with no eligible provider. With
two or more groups the worker runs one agent per group concurrently, up to `--parallel-agents`
(`TAUCETI_QUEUE_PARALLEL_AGENTS`, default 3; 0 or 1 disables the path), each in its own detached
worktree of the tip (`<root>/worktrees/task-<id>-group-<n>`, added from the task's worktree, with
`.lake/packages` symlinked to the task worktree's or to `--seed-packages` so no group extracts Mathlib
again, and built outputs restored from the private artifact pool) and each in its own child process
(`python -m tauceti_queue.repair_parallel run-group <job>`), which calls `run_repair_agent` exactly as
the sequential path does: the agent path applies its provider environment to the process and keeps
the last run's failure in module globals, neither of which threads could share. The worker chooses
the eligible providers once (`select_providers`) and every group's child takes that chain as given.
The group prompt (`group_prompt`) carries the instruction, the group's own evidence, the members and
files it may edit, the collisions section of the repair context, and a protocol instead of the chain
rewrite: fix in the working tree, `git commit --fixup=<sha>` for the owning member,
`git commit --allow-empty --squash=<sha> -m <explanation>` for the explanation, and leave those commits
on top of the tip; no rebase. The prompt is plain shell and python throughout, with no provider's tool
names in it, so the same text serves whichever provider runs the agent, and nothing depends on an
agent spawning helpers of its own (a provider that can is free to). Builds are serialized across the
worktrees by one lock, the task worktree's `.tauceti-repair/build.lock` (`repair_tools`): each group
worktree gets the same `.tauceti-repair/build` script the sequential agent has, written with that
shared lock baked in (`write_build_script(directory, lock=...)`), the agents are told to build only
through it, and the worker's verification build holds the lock through `repair_tools.build_lock`;
every one of these builds runs in the offline sandbox (`repair_sandbox.sandboxed_lake`), which binds
a group worktree's symlinked `.lake/packages` read-only from its target. When
every agent has finished the worker collects each group's commits (they must all be `fixup!` or
`squash!` commits, both kinds touching only the group's files, on a clean tree; anything else makes
the group `unusable`). Each target must uniquely identify an original member belonging to that
group. The worker cherry-picks the accepted changes to capture their combined reference tree,
then rebuilds the attributed members in order using Git object-store merges and commits. It
preserves each member's author, date and raw message, appends squash explanations, and retains
one commit even when a repair makes that member empty. The rebuilt tree must equal the reference
tree. It then validates the chain with `repair.chain_report` and runs the one whole-library
`lake build --iofail`. A group whose
agent failed, whose commits were unusable or did not apply, or whose module still fails that build
goes back to the sequential path: phase 4's usual agent runs on the task's worktree (now holding the
folded chain) with only the remaining findings, and the ordinary retry and release rules apply from
there. Every group's status, timing, worktree and agent record, the fold, the verification build and
the lock wait are in the outcome's `parallel` record and in the checkpoint (state `agent_running`,
label `parallel`), with each group's job, prompt, checkpoint and result under
`tasks/<id>/groups/<name>/` and its agent logs under `tasks/<id>/logs/<name>/`. A stop or lost claim
during the parallel phase terminates every group's child, which stops its agent, and the task is
abandoned.

## Inference unavailability

After an agent exit, `agents.take_last_agent_infra_failure` names the infrastructure reason when the
transcript is nothing but a provider failure: a transient status (408, 429, 5xx, 529), a transport
error, an exhausted credit balance or a subscription limit. Such a failure is retried up to
`inference_attempts` (3) with `inference_delays` (30 s, 120 s) between attempts, continuing the
recorded session when one exists; after the last attempt the task is released with
`inference_unavailable` and the last provider error. An agent that produced a transcript and failed,
or a chain that does not validate, is the agent's own failure and takes the `agent_failed` path; it
is never reported as inference unavailability.

## Providers

The agent's provider is chosen per task from a chain, `RunnerConfig.providers`
(`TAUCETI_QUEUE_PROVIDERS` / `--providers`, default `claude,codex,api`):

- `claude`: the Claude subscription, with the repository's Claude authoring profile
  (`resolve_authoring_profile("claude")`, `claude-opus-5-5` at effort `high`), `claude -p` with
  `ANTHROPIC_API_KEY` unset and the credential copied into the task's isolated `CLAUDE_CONFIG_DIR`.
- `codex`: the Codex subscription, with the repository's Codex authoring profile
  (`resolve_authoring_profile("codex")`, `gpt-6.1-sol` at effort `high`), `codex exec --json --model
  <model>` with the credential (`auth.json`) copied from `$CODEX_HOME` (default `~/.codex`) into the
  task's isolated `CODEX_HOME`, as `agents.isolate_home` seeds a fleet worker's. The prompt is the same
  text; past `agents.PROMPT_ARGV_LIMIT` it reaches `codex exec -` on stdin as it reaches `claude -p`,
  and the task sets the same `TAUCETI_SHELL_LIMIT_SECONDS` (two hours), which the prompt states; Codex
  has no shell-limit setting of its own (the fleet's Codex rounds get none either), so the prompt is
  what carries the limit there. The prompt names no CLI-specific tool, and the helpers it points at
  (`.tauceti-repair/check`, `step`) are plain Python and shell, so they run unchanged. The transcript
  goes through the same renderer as Claude's (`agents.run_agent_proc`): the same `agent-codex-*.log`
  shape, and `agent-codex-telemetry.json` with the turn's `input_tokens`, `cached_input_tokens` and
  `output_tokens`, which `repair.price_usage` prices for the attempt record and the submission notes.
- `api`: Claude billed to the API, the `TAUCETI_CLAUDE_BILLING=api` semantics: the same Claude
  profile, `ANTHROPIC_API_KEY` from the repair environment file set for this provider's agent only, in
  an isolated config dir that holds no subscription credential. The worker takes the key out of its own
  environment at start; it is never logged or recorded.

At the start of each run (`select_providers`) a subscription provider is eligible only if the fleet
pacer (`tauceti_worker.quota.Quota.claude` / `.codex`, the usage endpoints read with the host's
credential, their session and weekly windows against the pacing curve) reports it under pace, with
`RunnerConfig.rate` (`TAUCETI_QUEUE_RATE` / `--rate`, the worker's `--pace` spec) as that curve. The
read spends nothing and rotates no token. A subscription over its pace, with no credential, or with an
unreadable usage response is skipped with the pacer's reason logged; the `api` provider is skipped
without a key.

The eligible providers are tried in order (`run_repair_agent`). One that fails to launch, or exits
before doing any work with a refusal (`prework_failure`: for Claude a `rate_limit_event` status other
than `allowed` / `allowed_warning` or an API error on the first turn; for Codex its usage-limit,
rate-limit or authentication error event; for either an infrastructure failure the fleet classifier
names, or a missing executable) hands the task to the next provider at once, with no inference delay.
An agent that did work (a tool call, a tool result, an assistant message) is never replaced: its exit is
the task's, as before. When the whole chain fails before any work the pass is retried after
`inference_delays` up to `inference_attempts` times and the task released as `inference_unavailable`
(as `agent_launch_failed` when no provider's process could start at all). Every provider tried or
skipped is recorded in the run's `provider_attempts` (`provider`, `status` `ran` / `skipped` / `failed`,
`reason`, `seconds`), the run's `provider` names the one whose agent ran, the task record notes it,
and `worker.json` counts them per provider (`providers`) and repeats the last task's list
(`last_outcome.provider_attempts`).

## Stopped and abandoned tasks

Tasks are all-or-nothing. The task record `tasks/<id>/task.json` (written with
`runtime_status.atomic_json`) records the task, the claim token and expiry, the current phase and a
history of phases; `session.json` records the provider session for follow-up turns within the same
run (the explanation turn). Nothing continues a model session across runs; a rebase task's accepted
conflict resolutions are the one thing a rerun reuses ([Resuming after a stop](#rebase-conflicts-one-commit-at-a-time)).

A claim never outlives the worker that holds it. On SIGTERM, at any phase before the result is
validated, the attempt is abandoned and the claim released with reason `worker-stopped` so the
service re-exposes the task at once; the worktree is parked or removed ([One tree per batch](#one-tree-per-batch)). On start (`retry_deliveries`), every
record of this worker still in flight (`claimed`, `sources_ready`, `artifacts_ready`,
`artifacts_unavailable`, `agent_running`, `validated`, and `interrupted` from older releases) has its
claim renewed and then released as `worker-stopped`, its worktree parked or removed. Only a validated result
whose delivery was under way (`delivery_pending`, `pushed`, `submitted`) is delivered again: the push
to the submission ref and the `result` call are retried while the claim is live, and a claim the
service no longer recognises is recorded as `claim_lost`, so a lost claim never produces a late
submission. When a claim comes back empty while such a record of ours holds a live claim (the
service refused the claim for concurrency because of it), the stale claim is released with its
token and the claim retried at once rather than at the next poll.

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
run, `retry_deliveries(cfg, client)` for pending deliveries and abandoned claims. Integrating it as a work unit in
`tauceti_worker/work_units.py` needs:

- a unit that calls `agents.prepare_checkout` for the content checkout, builds a `RunnerConfig`
  with `clone=cfg.checkout`, `root=$TAUCETI_ACTIVITY_DIR/queue-repair` and the round's authoring
  profile, and runs `retry_deliveries` then `claim_and_run`; `None` from `claim_and_run` means nothing
  was claimable and the unit made no progress;
- the service URL and the token source as worker configuration (`TAUCETI_QUEUE_SERVICE`,
  `TAUCETI_QUEUE_TOKEN`);
- activity accounting from the outcome record (`status`, `agent[*].attempts[*].seconds` and
  `estimated_cost_usd`, `lease.renewals`, `response`), and the `agent-<provider>-*-telemetry.json` files
  the host agent writes under `tasks/<id>/logs/`;
- the round's own deadline: the task sets `TAUCETI_ROUND_DEADLINE` from `agent_timeout` for the
  agent's shell limits, and a loop that owns the round should pass its remaining time as
  `agent_timeout`.

The service's `repair_concurrency` setting bounds live claims across distinct tasks. Each task has
one live owner: one worker repairs the head while another prepares the next eligible batch. A spare
worker waits if no separate task is claimable. Expiry or release allows another worker to take over;
the runner itself claims one task at a time.

## Tests

`tests/queue_repair_task.py` (registered in `tests/run-all`) runs the task against a fake service
(`http.server` in a thread with the controller's task semantics: claims with expiry within a
concurrency of one, renewals, submissions validated with the chain tool against the bare repository
in the controller's order, `explanation-required`, the `release` call, the `Adoption` shape and
reason codes, encoded as stream F's server does, with scripted responses where a scenario needs
them), a local bare content repository with a three-member attributed chain whose second member
breaks the build, a `lake` stub that fails modules containing `BROKEN`, a `gh` stub for the admin
predicate and a scripted `claude` stub that rewrites the constituent commits (or, given a group
prompt, leaves fixup commits). It covers adoption on the happy path, claim expiry stopping the agent without a submission, renewal errors that do not
stop it, `invalid-chain` retried once with its violations, `explanation-required` resubmitted from
the same session, a terminal rejection, a superseded claim at submission, a locally invalid chain
retried with the local report, the local explanation follow-up turn, artifacts unavailable with and
without a `release` route, inference unavailable after bounded retries, an ordinary agent failure
and the worker's attempt cap released as `agent_failed`, a second claimant and a stale token refused,
abandonment of a stopped task, the task record and prompt for a rebase conflict, the numproj
pre-pass with a stand-in tool (`NumprojPrepass`: a projection-only conflict resolved without the agent
in two sandboxed runs, a real conflict and sites left by the tool handed to the agent from the
rewritten merge, and a tool failure, a missing tool, a rewrite that changes more than projections, an
edit the tool would not make, a symlink planted under `.lake`, a failed write, an unacceptable mechanical
resolution and a cold tree all leaving git's own conflict to the agent), the
provider chain (the same stub installed as `codex`: chain order, a subscription over pace or
unmeasurable skipped, a launch failure or a refusal before any work handed to the next provider, the
`api` provider's key and credential-free config dir, the attempts in the outcome and the heartbeat, and
the eligibility check against the pacer's readers), the ownership and declaration context
(declarations under namespaces, a collision and a rename ripple reported with their members, and a
120-member batch whose prompt section stays under the cap), and the repair cache publication (the
`--no-build` map then `lake cache put` with the key from the key file and never in the agent's
environment, the repair namespace fetched for tip and base before the public cache, a failed upload
that still submits, and no upload without a key, when switched off or when the outputs are not
green). For the build sandbox, a recording `bwrap` stub checks the composed binds, `--unshare-net`
and the cleared environment, the disabled fallback's warning, failing closed without `bwrap`, and the
`--check` line. The parallel path, with members #2 and #3 both broken: two group agents running at the
same time in their own worktrees and child processes (under the Claude and the Codex argv shapes) with
fixups folded into a valid chain that is adopted, the groups taken from the task's diagnostics without
waiting for the streaming build, every build serialized by the shared lock and run through the
sandbox wrapper, a group whose agent fails (or whose fixups stray outside its files) handed to the
sequential agent, a lost claim stopping the group agents, the build lock serializing two concurrent
scoped builds and taking over a stale lock, a provider-neutral group prompt, and dependent or
unassignable failures staying sequential. A batch's second task adopts the first task's parked tree
and starts warm without fetching. A `prepare` task whose batch builds is released as
`prepared_clean` without an agent, and one whose batch fails is repaired with the not-yet-tried
prompt and adopted. No real inference and no network.

## Throughput and delivery recovery

When the next batch's intended parent is available, prepare directly on that parent rather than
requiring an old-base full build. Already claimed preparations finish normally. Rebase agents resolve
one conflicting commit and perform focused checks; the worker owns the whole-library verification.

`checks-timing.json` records each check's wall time and sampled process-tree RSS. The checks always
run at the same time, the compiled ones over read-only build outputs, and all are joined before task
cleanup with every failure collected. The audit and the environment lint each sampled 21 to 27 GB of
RSS in 2026-11 (mostly the same mapped `.olean` files, counted in both), so together they take
roughly the time of the environment lint alone; the repair slice's live `memory.high` bounds their memory. The pinned lint driver records import, docscan,
linters and report times; `TAUCETI_LINT_PROFILE=1` additionally profiles each linter separately.
These timing observations never authorize a pass.

The worker tests submission Git access before inference. A verified tip whose push/submission fails
stays in the existing `delivery_pending` checkpoint and retained ref; polling renews its claim and
retries delivery with bounded individual attempts. Unchanged source and checking identity reuse
verification. A changed checking policy requires verification again. The controller replays an
accepted receipt only for the exact claim and tip, so a lost response cannot adopt twice or authorize
stale work. Terminal worktrees are removed as before.

Staging status intents persist for the existing retry workflow. A green build waits for required
status delivery on its original run. The finalizer still requires the build and exact public map.
Legacy missing-run grace is unchanged pending duplicate-safe dispatch evidence.

For a repair that compiles and still fails audit or lint after local retries, the worker
can send a checkpoint using the current claim and its submission ref. The controller validates the
chain and records it as a retained submission without adopting it. A new claim on the same task
starts from that tip and receives the outstanding findings. It runs full verification before final
submission. The old worktree is removed once the checkpoint receipt is confirmed.

When a pre-rebase passes full verification, including the whole-library lint, the worker reports
the verified tip, parent and tools pin with its submission. A matching result counts as
preparation for that tip. A changed chain gets its own preparation and staging still makes the landing decision.

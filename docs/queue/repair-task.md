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
    [--providers claude,codex,api] [--rate T:B[,T:B...]] [--parallel-agents N]
```

Without `--once` the runner is the long-running worker (`serve`): resume checkpointed tasks, claim,
run, submit, report the provider outcome to `POST /tasks/inference` (a success lifts the service's
inference hold; a release for `inference_unavailable` counts toward it), sleep `--poll` seconds when
nothing is claimable, and log and retry rather than exit when a task, the service or the
authorization check fails. SIGTERM and SIGINT stop a running agent and record the task as
`interrupted` with its claim and worktree kept; the next start resumes the session
(`resume_pending`). It writes a heartbeat to `<root>/worker.json`. `--check` prints the host checks
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
| `artifacts_unavailable` | the worker could not prepare warm artifacts (no published cache mapping for the base or `onto`, a fetch failure, an incomplete incremental build, or the worker's own cache preparation refusing to start inference); the task was not run cold. After such a release `serve` waits `artifacts_backoff` (60 s, doubling to 600 s across consecutive releases) before its next claim, since the service re-exposes the same task at once |
| `inference_unavailable` | every bounded attempt failed with a provider error, a transport failure or exhausted credentials (`agents.classify_agent_failure`); the queue holds the task and re-exposes it (plan, "Inference unavailability") |
| `agent_launch_failed` | the agent process could not be created at all (`OSError` from `Popen`: a missing executable, an argv the kernel rejects); the artifacts were ready, so no `artifacts_backoff` applies, and the error names the launch as the cause. A prompt longer than `agents.PROMPT_ARGV_LIMIT` (64 KiB) is written to the agent's stdin rather than passed in argv, since Linux rejects a single argument above 128 KiB |
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
   --rev=<revision>` into the private pool, then `lake build --iofail` at the tip, which restores every
   unchanged module and builds the batch's own modules and their dependents (for a repair with
   streaming on, the default, that build is the worker's background build described after this list,
   and the preparation ends with the fetch). The artifacts come from
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
   says what happened in plain words (for a rebase conflict: the queue replayed this batch of N squash
   commits onto the current `main` commit with git, the replay stopped at PR N's commit with conflicts
   in these files, the commits before it replayed cleanly and are at this commit, the job is to finish
   the replay; for a repair: the batch was built on `main` with CI and the build failed), the evidence
   with the git refs the agent needs named once (base, target, the conflicting commit, the clean
   prefix, and for every file the owning commit: "PR N's commit `sha` modifies this file, and `main`
   has changed it since the batch's base"; for a repair the compiler, audit and lint findings with
   their owning commits), the result the queue expects (a linear chain on `onto` with one commit per
   PR in the same order, each keeping its `Source-PR:` and `Accepted-Head:` trailers and its ` (#N)`
   subject suffix; the queue reads it from the worktree, validates it and runs CI itself, so there is
   nothing to push or report and `gh` is not needed; no changes introduced to the build configuration,
   `scripts/` or `.github/`, though a constituent commit that modifies such files keeps its change),
   an "Already on main" section when members' PRs have landed on `main` outside the queue since the
   batch's base (one line each with PR number, title and landing commit, up to `repair.LANDED_SHOWN`,
   the rest by count; such a member becomes an empty commit and a conflict in one of them takes
   `main`'s version; the list comes from the members' `landed_externally` flag when the service sends
   one, else from the subjects ending in ` (#N)` between base and target), one editing instruction
   (for a rebase conflict: `git rebase --keep-empty --empty=keep --reapply-cherry-picks --onto <target>
   <base>`, where `--reapply-cherry-picks` keeps a member whose identical patch is already on `main`
   as an empty commit instead of dropping it; resolve each stop inside the constituent commit with
   `git add`, `git commit --allow-empty -C REBASE_HEAD` (the member's own message and author, kept
   when the resolution leaves it empty) and `GIT_EDITOR=true git rebase --continue`; explanations
   appended with `git commit --allow-empty --squash=<sha> -m <explanation>` and content changes with
   `git commit --fixup=<sha>`, folded in by an autosquash rebase that keeps each commit's message and
   author), and local verification. The member listings are not in the prompt (1,197 members once
   made a 291 KB prompt, about 75k tokens of noise): `repair.write_listings` writes
   `.tauceti-repair/members.jsonl` in the worktree, one JSON object per member in chain order with
   `position`, `commit`, `pr`, `title`, `accepted_head`, `provenance`, `landed_on_main` (the landing
   commit, else null) and, for a rebase conflict, `state` (`replayed`, `conflicting` or `pending`),
   and beside it `.tauceti-repair/check`, the agent's one validation command: a standalone Python
   script that verifies the chain is linear on the target, has exactly one commit per member in the
   recorded order with the trailers and ` (#N)` subjects kept and every original message preserved as
   a prefix, that no rebase is in progress and that the working tree is clean, and prints the first
   problem or `ok`. A rebase conflict also gets `.tauceti-repair/step`, a short shell script that
   starts the rebase (or continues it), commits each stop whose member is already on `main` with
   `main`'s version of the conflicted files as an empty commit, stops at the first real conflict
   naming the commit and the files, and refuses to start when HEAD already sits on the target. The
   directory is excluded from Git through the clone's `info/exclude`, so no recipe the agent runs can
   commit it, and `checkout_worktree` leaves it alone; a prompt composed without a worktree (the
   experiment's preview) carries the listings inline. The verification advice follows
   the artifacts outcome: with warm artifacts it describes the incremental whole-library build; when a
   rebase conflict runs without them (`cold`) it says so, gives the reason, warns that
   `lake build --iofail` starts from nothing and may take hours, and asks for the touched modules
   (`lake build <Module>`) to be built first. Either way the prompt tells the agent to check a fix
   with the scoped build first and to run the whole-library `lake build --iofail` only as the final
   verdict: `.tauceti-repair/build` (written by `repair_tools.write_build_script` beside the check
   script, a Python script of under sixty lines) takes module names (`TauCeti.Foo.Bar`) or paths
   (`TauCeti/Foo/Bar.lean`), with `--dependents` every module importing them transitively (from the
   library's `import` lines, read in one pass and cached in `.tauceti-repair/imports.json`, which the
   worker drops whenever it rewrites the script), and runs `lake build --iofail` on them in the
   foreground, exiting with Lake's status; with no arguments it builds the whole library. Every build
   in the worktree holds the lock directory `.tauceti-repair/build.lock`, taken atomically by `mkdir`
   with the holder's pid in `pid` inside: a waiter polls every 2 s, prints a line every 30 s, and
   clears a lock whose holder has died. The worker's own builds hold the same lock through
   `repair_tools.build_lock`, so exactly one `lake build` runs in a worktree at a time whoever
   started it. Long builds run in the foreground: the task
   sets `TAUCETI_SHELL_LIMIT_SECONDS` to `RunnerConfig.shell_limit` (two hours;
   `repair.SHELL_LIMIT_SECONDS`) for the agent's shell limit, where the fleet's authoring rounds keep
   ten minutes, and the prompt says so. The prompt states the aim (every constituent PR's mathematics
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
   renames" section, next to "Already on main", lists them with member positions, PRs and files up
   to `repair_context.SECTION_LINES` bullets and points at that file for the rest. One `git
   diff-tree` per member and `git cat-file --batch` for the changed blobs run over a thread pool, so
   a 1,197-member head takes about a second; a failure in this step is recorded in the outcome and
   the prompt simply lacks the section.
5. **Checks.** `repair.chain_report`, the controller's own checks in its order: `chain.validate`
   against the recorded members on `onto` (identity by `Source-PR`, single-parent history, both
   trailers, count) and `chain.compare` of the input chain `base..tip` against the result on `onto`
   (append-only messages, provenance per member). A chain that validates but changed a member's
   content without an appended explanation gets one short follow-up turn in the same session
   (`--resume`, `repair.explanation_prompt`) asking only for the explanation, as the plan requires of
   the same attempt. A chain that fails validation, a dirty worktree, or a HEAD still at the input
   tip is not acceptable: one bounded retry (`agent_attempts`, 2) with the local report and the
   retained input ref appended to the prompt, then `agent_failed`. Checkpoint `validated`.
6. **Publish and submit.** Publish the build outputs (below), push, `result`, record the response (checkpoints `pushed`, `submitted`,
   then `adopted` or `not_adopted`). The worktree is removed afterwards unless `--keep-worktree`;
   the artifact pool is kept, since the next task on the same base reuses it (prune it with the
   experiment CLI's `prune`). Every attempt's tip stays under
   `refs/tauceti-queue/task/<id>/attempt-<n>` in the clone, and `tasks/<id>/outcome.json` holds the
   outcome record.

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
When the build ends the worker appends a `build finished, exit N, M errors total` line to the error
list, removes the lock and copies the log to `tasks/<id>/logs/stream-build.log`; the outcome records
the build under `stream_build`. The agent's final whole-library `lake build --iofail` after its fixes
is still the only verdict. A build still running when the task ends or is interrupted is stopped and
its lock removed. Rebase-conflict tasks, resumed tasks and `--no-stream-repair` keep the sequential
flow: the preparation's own incremental build, then the agent.

## Repair cache publication

After a chain passes the local checks and before it is pushed, `repair_publish.publish_outputs`
publishes the worktree's build outputs under the tip's revision to a cache namespace only the workers
read, so the next repair round on the same head and the next head's agent start warm instead of
rebuilding what this repair built. `lake build --no-build --rehash -o <map>` writes the output map and
exits nonzero unless every module is built and current at the tip, so only a green final build is
published; `lake cache put <map> --service tauceti-repair-upload --repo <repository>` then uploads the
map and its artifacts with a bounded timeout (`TAUCETI_REPAIR_CACHE_PUBLISH_TIMEOUT`, default 1800 s),
and the outcome's `publish` record holds the artifact count, bytes and seconds. A failure or a skip
is logged and recorded and never fails the task. The namespace is two services the worker writes
into the pool's `lake.toml`: `tauceti-repair` reads from `TAUCETI_REPAIR_CACHE_URL` (public, since Lake
fetches without credentials) and `tauceti-repair-upload` writes to `TAUCETI_REPAIR_CACHE_UPLOAD_URL`
(the S3 endpoint of the same bucket; default the read URL), each holding `artifacts/` and `revisions/`
laid out like the public cache. The write key, the `access-key-id:secret` pair Lake takes as
`LAKE_CACHE_KEY`, is read from the file named by `TAUCETI_REPAIR_CACHE_KEY_FILE` and given only to the
`lake cache put` process; `LAKE_CACHE_KEY` is removed from the environment the agent inherits. Use a
bucket of its own with an R2 token scoped to it, so the worker's key cannot write the public cache
CI publishes to (`LAKE_CACHE_KEY` in TauCeti's `publish-lake-cache.yml`). `TAUCETI_QUEUE_PUBLISH_CACHE`
(`1`/`0`) switches publication on or off; unset, it is on when the key file and an upload URL are
configured. CI never fetches from this namespace, so its verdicts are unchanged.

## Parallel agents within one task

Before the agent of phase 4 runs, a repair task's findings are split into independent groups
(`tauceti_queue/repair_parallel.py`, `plan_groups`). When the worker streamed the first build, the
findings are the task's diagnostics merged with that build's complete error list
(`.tauceti-repair/errors.jsonl`, each error with its file, line, column and message; `read_errors`,
`merge_errors`): the path waits for the streaming build to finish first, since a group is independent
only against the whole list, and the outcome's `parallel.source` names `errors.jsonl` with the time
waited. (With `--parallel-agents 1` the agent is launched at the first error as the streaming
section describes; with the default of 3 a repair trades that head start for concurrent agents on the
complete list.) Two failing modules share a group when one
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
`squash!` commits, the fixups touching only the group's files, on a clean tree; anything else makes
the group `unusable`), cherry-picks them onto the tip in the task's worktree, folds them with
`GIT_SEQUENCE_EDITOR=: git rebase -i --autosquash --keep-empty --empty=keep <base>`, validates the
chain with `repair.chain_report` and runs the one whole-library `lake build --iofail`. A group whose
agent failed, whose commits were unusable or did not apply, or whose module still fails that build
goes back to the sequential path: phase 4's usual agent runs on the task's worktree (now holding the
folded chain) with only the remaining findings, and the ordinary retry and release rules apply from
there. Every group's status, timing, worktree and agent record, the fold, the verification build and
the lock wait are in the outcome's `parallel` record and in the checkpoint (state `agent_running`,
label `parallel`), with each group's job, prompt, checkpoint and result under
`tasks/<id>/groups/<name>/` and its agent logs under `tasks/<id>/logs/<name>/`. A stop or lost claim
during the parallel phase terminates every group's child, which stops its agent; a resumed task
restarts sequentially from the tip.

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
without a key. A resumed session's provider is not re-checked: the work was admitted when it started.

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
`reason`, `seconds`), the run's `provider` names the one whose agent ran, the checkpoint records it for
resumption, and `worker.json` counts them per provider (`providers`) and repeats the last task's list
(`last_outcome.provider_attempts`).

## Recovery

The checkpoint `tasks/<id>/task.json` (written with `runtime_status.atomic_json`, the style of
`repair_recovery.py`'s journal) records the task, the claim token and expiry, the current phase and
a history of phases; `session.json` records the provider session. On start, `resume_pending`
looks for checkpoints in `agent_running`, renews their claim, and when the claim is still the
worker's resumes the same model session in the same worktree with a short continuation prefix on the
original prompt, skipping source and artifact preparation. A claim the service no longer recognises
is recorded as `claim_lost` and not resumed. Bookkeeping loss therefore never restarts a model
session while the claim is live, and a lost claim never produces a late submission.

A claim never outlives the worker that holds it. On SIGTERM with the agent running, the claim is
renewed and the task checkpointed as `interrupted` for the next start to resume; a stop before the
agent ran (fetching sources, preparing artifacts) releases the claim with reason `worker-stopped`
so the service re-exposes the task at once, and the log says which. On start, every checkpoint of
this worker still in flight (`claimed`, `sources_ready`, `artifacts_ready`, `artifacts_unavailable`,
`agent_running`, `validated`, `pushed`, `submitted`, `interrupted`) has its claim renewed: a
resumable one (`agent_running`, `interrupted`) continues, any other is released as `worker-stopped`
and the task claimed fresh. When a claim comes back empty while such a checkpoint of ours holds a
live claim (the service refused the claim for concurrency because of it), the stale claim is
released with its token and the claim retried at once rather than at the next poll.

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
  `estimated_cost_usd`, `lease.renewals`, `response`), and the `agent-<provider>-*-telemetry.json` files
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
predicate and a scripted `claude` stub that rewrites the constituent commits (or, given a group
prompt, leaves fixup commits). It covers adoption on the happy path, claim expiry stopping the agent without a submission, renewal errors that do not
stop it, `invalid-chain` retried once with its violations, `explanation-required` resubmitted from
the same session, a terminal rejection, a superseded claim at submission, a locally invalid chain
retried with the local report, the local explanation follow-up turn, artifacts unavailable with and
without a `release` route, inference unavailable after bounded retries, an ordinary agent failure
and the worker's attempt cap released as `agent_failed`, a second claimant and a stale token refused,
resumption of a checkpointed session, the task record and prompt for a rebase conflict, the
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
fixups folded into a valid chain that is adopted, the groups taken from the streaming build's
`errors.jsonl` once it has finished, every build serialized by the shared lock and run through the
sandbox wrapper, a group whose agent fails (or whose fixups stray outside its files) handed to the
sequential agent, a lost claim stopping the group agents, the build lock serializing two concurrent
scoped builds and taking over a stale lock, a provider-neutral group prompt, and dependent or
unassignable failures staying sequential. No real inference and no network.

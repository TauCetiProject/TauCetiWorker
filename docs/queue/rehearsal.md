# Queue cutover rehearsal

Stream I of the [AI merge queue plan](../redesign/specs/ai-merge-queue.md) ("Transition to
production", step 2): the full increment 1 flow executed on synthetic pull requests before any
production step. The operator dropped the shadow period, so this is the only end-to-end check before
the cutover. `tauceti_queue/rehearsal.py` drives it; `scripts/queue-rehearsal` is the wrapper;
`tests/queue_rehearsal.py` runs the same scenario script over the fakes on every CI run.

Two rigs run one scenario script:

| Rig | GitHub | CI | Service | Worker path |
| --- | --- | --- | --- | --- |
| `local` (the dry run, `tests/queue_rehearsal.py`) | `tauceti_queue.fakes.FakeGitHub` over a bare repository, approvals as signed webhook deliveries to the real server | scripted runs completed by the rig, a `lake` stub that fails modules carrying `REHEARSAL-FAIL` | the real controller, `server.py` and `loop.py` in-process, restarted in-process | the real `repair_task.py` runner against the real HTTP task API, with the scripted `claude` |
| `live` | `TauCetiProject/TauCetiQueueScratch` through the `tauceti-queue` App (5607007); `bors r+` comments and admin calls with the operator's token | real `pr-build.yml` runs on `ubuntu-latest`, `lake build --iofail` with the pinned toolchain | a local `tauceti-queue serve` process on its own port and database, `publication_enabled = true`, restarted by SIGTERM | the same runner against that service, pushing results through `git-safe-push` with the operator's git credentials |

Nothing in either rig writes to `TauCetiProject/TauCeti`. The live rig writes only to the scratch
repository (branches, PRs, comments, statuses, check runs, the `refs/tauceti-queue/*` namespace and
direct pushes to `main` that play the administrator) and, through the App API, reads the App's
webhook delivery log.

## Running it against the scratch repository

1. **Operator: create the repository.** `TauCetiProject/TauCetiQueueScratch`, empty (no README is
   fine), Actions enabled, default branch `main`, no branch protection. Public or private both work.
2. **Operator: add it to the App installation.** On the `tauceti-queue` App's installation settings
   for `TauCetiProject`, add `TauCetiQueueScratch` to the repository access. The rehearsal refuses to
   start until `GET /installation/repositories` lists it.
3. **Operator (optional): grant `checks: write`.** The service approves PRs the way TauCetiReview
   does today, by reading a `merge eligibility` check run. Publishing one needs `checks: write`, which
   the App was registered without (`checks: read`). With the permission granted and accepted on the
   installation, the rehearsal publishes the check under the queue App's own identity and the local
   service is configured to trust that App id (`review_app_id`, a new configuration key, defaults to
   TauCetiReview's 3947238). Without it the `eligibility` scenario is skipped with that reason and the
   PR is approved by comment instead; nothing else changes.
4. **Operator (optional): activate the App webhook.** If the App's webhook is active (it points at
   `queue.taucetiproject.org`), the rehearsal relays GitHub's own deliveries for the scratch
   repository from the App's delivery log into the local service, signed with the stored secret;
   the production service ignores them as an unserved repository. If it is inactive, the rehearsal
   synthesizes the deliveries for the actions it performs itself (comments, closes, pushes, check
   runs) from the API objects GitHub returned, and the service's polling observes CI. The report says
   which (`facts.webhooks`).
5. **Run it** on a host with the App credentials in `~/.config/tauceti-queue/`, `gh` logged in as a
   repository admin (the operator), the pinned toolchain installed (`elan` resolves
   `leanprover/lean4:v4.36.0-rc2`), and `git` able to push to the scratch repository through `gh`'s
   credential helper. The scratch repository must be reachable by the organisation's Namespace runners
   (the scaffold's jobs run on `nscloud-ubuntu-24.04-amd64-2x4`; GitHub-hosted labels never got a runner
   here), and the service must be reachable from those runners over HTTPS: the scaffolded workflow
   is TauCeti's staging path, whose train identity check and finalizer read the Bors observation feed
   at the `services.bors.url` of the scratch repository's `tauceti.toml`, which the scaffold sets to
   `--public-url` (default `https://queue-rehearsal.taucetiproject.org`, a Cloudflare tunnel hostname
   forwarded to the service's local port on the host; TauCetiTools accepts HTTPS only, so the local
   listener itself can never be configured). The same value is the service's `public_url`. While
   nothing answers there, every candidate run fails closed in "Resolve PR head" with
   `bors identity unavailable` and the `clean` scenario fails:

   ```sh
   cd ~/TauCetiWorker   # any checkout of this repository
   uv run scripts/queue-rehearsal live --repository TauCetiProject/TauCetiQueueScratch \
       --public-url https://queue-rehearsal.taucetiproject.org \
       --port 8781 --workdir /tmp/queue-rehearsal-live --report /tmp/queue-rehearsal-report
   ```

   The rehearsal then: checks the repository, the installation, the App permissions and the operator's
   admin permission; pushes the scaffold (the Lean library, `pr-build.yml`, `tauceti.toml` and the
   `scripts/` entrypoints, see below) to `main` when it is absent or out of date; starts
   `tauceti-queue serve` with a generated `config.toml` (repository, `canonical_branch`,
   `publication_enabled = true`, a private content clone and database under the work directory, the
   App credentials, port 8781, `public_url` from `--public-url`) and lowers the polling and retry
   intervals through the admin endpoint; installs the scripted `claude` and the `lake` wrapper on a
   private PATH; starts the webhook relay; and runs the scenarios. `--service URL` uses a service
   you started yourself instead (then the restart scenario is skipped), `--only a,b` runs a subset,
   `--keep-service` leaves the service running for inspection. Expect 40 to 60 minutes: about twenty
   `pr-build.yml` runs of one to two minutes each, sequentially, plus the finalizer's deliberate wait
   in the stale-status scenario.
6. **Read the report**: `/tmp/queue-rehearsal-report/rehearsal-live.json` and `rehearsal-live.md`
   (below). Attach the Markdown to the PR or the status page.

The scaffold alone: `uv run scripts/queue-rehearsal scaffold PATH` writes the content for inspection.
The dry run alone: `uv run scripts/queue-rehearsal local` (what the test runs), report under
`/tmp/queue-rehearsal-report/rehearsal-local.*`.

## The scratch repository's content

Generated by `scaffold`, pushed by the live rig when `.rehearsal/VERSION` or any generated file
differs from this version (the shared module is scaffolded once and then evolves with every run):

- `lakefile.toml`: package `TauCeti`, `platformIndependent = true`, one library `TauCeti` with
  `globs = ["TauCeti.*"]`, no dependencies. The library keeps TauCeti's name because the queue's
  diagnostics reader (`tauceti_queue/diagnostics.py`) recognises `TauCeti/...lean` paths and the repair
  prompt's module conventions assume it.
- `lean-toolchain`: `leanprover/lean4:v4.36.0-rc2`, TauCeti's pin; a build takes under a second.
- `TauCeti.lean` (empty root, as TauCeti's), `TauCeti/Basic.lean`, `TauCeti/Logic.lean` (simple
  theorems), `TauCeti/Shared.lean` (the conflict target: a definition at the top, an append marker at
  the bottom). Every run adds its modules under `TauCeti/R<timestamp>/` in namespace `R<timestamp>`,
  so runs never collide.
- `.github/workflows/pr-build.yml`, adapted from TauCeti's and read against it on 2026-11-04, with
  TauCeti's staging path: the workflow-pinned tooling checkout into `gate/`, TauCetiTools' `setup`
  and `ci-credentials` actions at the pin `tauceti.toml` names, `bors_train_identity.py` (the
  observation feed's `train.staging_ref` must be `staging-candidate-<candidate_id>-<generation>`)
  and `ci_git.py resolve` (that branch's tip must be the dispatched head) in "Resolve PR head", an
  identity failure reported as infrastructure without building, and `finalize_batch.py` posting the
  terminal status after revalidating the batch against the feed. The first production head failed in
  that step while the earlier scaffold, which lacked it, passed. What the service relies on is
  identical: the `repository_dispatch` event `tauceti-bors-staging` with
  `client_payload.head_sha`, `base_sha`, `batch_id`, `members`, `base_ref`; the same `run-name`
  (`bors branch=<ref> batch=<id> head=<sha> base=<sha>`) the adapter finds the run by; the same
  concurrency group; a build job named `sandboxed-build` (the job whose log the adapter reads for
  `error: TauCeti/...lean:line:col:` diagnostics and whose runner it reports); and a
  `finalize-merge-group-build` job that posts the `build` commit status on the candidate head with
  `target_url` `https://github.com/<repo>/actions/runs/<run_id>`, which the service binds its verdict
  to. `pull_request` events build PR heads and post `build` on them, as TauCeti does, so statuses from
  PR builds exist beside candidate statuses. Dropped: bwrap, caches, cache publication, audits, lint,
  scope and bump checks, runner picking. Added, for one scenario only: a candidate carrying
  `.rehearsal/suppress-build-status` with a number N keeps the finalizer silent while fewer than N
  `build` statuses exist on the candidate (and waits 150 s so a stale status can be planted); a
  candidate without the file is reported exactly as TauCeti's finalizer reports it.
- `tauceti.toml` (the project configuration TauCetiTools reads in CI: this repository as content,
  Lean root `TauCeti`, `merge.backend = "bors"`, `services.bors.url` from `--public-url` with
  `repository_id = 1`, the TauCetiTools pin) and `scripts/bors_train_identity.py`,
  `bors_observation.py`, `finalize_batch.py`, the compatibility entrypoints TauCeti's finalizer
  sparse-checks-out; CI materializes the implementations over them. The workflow references the
  `CI_READ_APP_*` and `CI_STATUS_APP_*` secrets as TauCeti's does; the scratch repository has none,
  and the credentials action falls back to the workflow token.

## Synthetic agents

The worker path is real wherever the service is involved: `tauceti_queue.repair_task.claim_and_run`
claims through `POST /tasks/claim`, renews, prepares artifacts, runs the agent in a private worktree,
checks the chain with the chain tool, pushes to the submission ref and submits; the service validates
and adopts. Two stand-ins sit under it:

- `claude` is a scripted agent (`CLAUDE_STUB` in `rehearsal.py`), installed first on PATH and named
  through `TAUCETI_CLAUDE_CMD`. It reads the task's base from the prompt's rebase recipe, replays the
  chain commit by commit onto it, applies a plan (`REHEARSAL_AGENT_PLAN`: per `Source-PR`, files to
  write, `empty`, and the explanation to append), keeps authors and messages, runs
  `lake build --iofail`, and prints the stream-json lines the host path reads. In mode
  `provider-failure` it prints `API Error: 529 Overloaded` and exits 1, which the runner classifies as
  inference unavailability and releases with `inference_unavailable`.
- `lake` is a wrapper that answers `lake cache get` and `lake exe cache get` locally (the scratch
  library has no published cache and no Mathlib) and otherwise runs the real `lake` (live) or a stub
  that fails modules carrying `REHEARSAL-FAIL` (local).

`fakes.ScriptedAgent` is not used: every repair, shrink and conflict resolution in both rigs goes
through the runner. Inference is never called.

## Scenarios

Each record carries the plan's acceptance scenario numbers it covers. The order is fixed; a failure
stops the run (later scenarios are recorded as skipped), because the queue's state carries over.

| Record | Proves | Evidence recorded |
| --- | --- | --- |
| `clean` (1, 4, 5, 8) | two PRs approved, inserted, sealed, built and landed with no agent task; the open batch never builds | PR heads, approval comment or check ids, head ids, versions, base and tip SHAs, run ids and URLs, closed PR titles (`[Merged by Bors] - `), landing comment ids, the agent call count (0) |
| `eligibility` (1) | approval by a `merge eligibility` check run, the way TauCetiReview approves | the check id, the App id it was published under, the merge base; or the skip reason |
| `source-failure` (2, 4, 5, 11) | a Lean module that fails `lake build` makes a repair task with the compiler diagnostics; the real runner repairs it in place; the replacement validates; the landing comment quotes the explanation and the clean batch-mate's comment does not | failed run id, task id and diagnostics, adoption (version, tip, pushed ref), member provenance, comment ids and excerpts |
| `shrink` (3, 14) | a hopeless member is reduced to an empty commit, the batch lands, the comment names what was dropped | the empty commit record from chain validation, the counter, the comment |
| `conflict` (2, 9, 10) | an administrative push to `main` rebases the running head and re-dispatches it without a merge commit; a member conflicting with that advance becomes a rebase-conflict task resolved by the runner; a PR that did not replay cleanly onto the open batch (its probe conflict is journaled) is probed again after its batch-mate lands and inserts without an agent | the advance SHA, head versions and run ids before and after the rebase, the probe record (`probes`, `last_probe`), the task kind and diagnostics, the adoption, the journal entries `probe-conflict` and `inserted`, the final shared module |
| `urgent` (18, 20) | an urgent PR marked through `/admin/urgent` is in the next head; the running head's run is the one that lands it (never cancelled); the next head lands by promotion | the running run id, the open batch's members and urgent flag, the `ci-pass` and `landed` (`how: promoted`) journal entries |
| `stale-status` (10, 19) | a `build` status bound to a superseded run is not a verdict: the attempt becomes `infrastructure` (`missing-status`), the candidate and its approvals are unchanged, and the head is re-dispatched and lands on the new run | the planted status, the refused attempt's outcome and detail, the `ci-infrastructure` journal entry naming the stale run, the second run id, the landing |
| `cancelled-run` (19) | a cancelled candidate run leaves the candidate unchanged and is re-dispatched | run ids, outcome details |
| `closed-member` (16, 21) | a member closed by an automation after admission still lands; closure is not withdrawal | the close, the `member-event-ignored` journal entries, the PR state and landing comment, the content on `main` |
| `inference-hold` (13) | three releases with `inference_unavailable` from the real runner put the queue on hold (status reports it as a reason); membership and the head are unchanged; insertion continues; the first adopted task lifts the hold | the releases, the hold record, the insertion during the hold, the adoption, the counters |
| `restart` (7, 12) | a SIGTERM and restart of the service mid-build recovers the same head, version, tip and run without a repeated dispatch, and the head lands | service start times and revisions, the head and run before and after, the dispatch counter |
| `chain` (3) | every landed head validated with `chain.validate`: single-parent, both trailers, bijection with the members, empty commits kept | per head: base, tip, the commit records |
| `feeds` (22) | the observation and candidates feeds (including `promoted=true`) validate against the committed Bors schema before the first approval, during every head's build and after the last scenario | each check's location, batch ids and states, error list |
| `status` (22) | status names the running revision and the runner and host of every candidate build | the revision, the runs |

Not expressible on the scratch repository: scenario 15 (live activation) and the Bors import half of
24, which need Bors and production inputs; 17's 110-PR timing, a pilot measurement; 23 (increment 0).

## Reading the report

`rehearsal-<mode>.json` (`tauceti-queue.rehearsal/v1`): `ok`, `counts`, `facts` (the rig's
findings: repository id, installation repositories, App permissions, operator and permission, the
scaffold push, the webhook mode, service starts and stops, relay counts, the GitHub client's request
and rate-limit counters) and `scenarios[]` with `id`, `title`, `plan`, `status` (`pass`, `fail`,
`skip`), `detail` (the failing check or the skip reason) and `evidence`. `rehearsal-<mode>.md` is the
same as a table followed by every scenario's evidence block. The exit criterion of the plan's step 2
is every record `pass` or `skip` with a documented reason (`eligibility` without `checks: write` is
the one expected skip).

## Findings

From the live run on the scratch repository (2026-11-04, report in `rehearsal-live.md`), each fixed
in the code or the runbook:

- Retained refs were created through the Git Data API, which cannot point at a commit GitHub does
  not have; the candidate tip exists only in the service's clone, so every dispatched run failed at
  checkout and ended as `missing-status`. `update_ref` now pushes `+<sha>:refs/<ref>` from the clone
  and the controller pushes the head's ref before each dispatch. The fake pushed objects implicitly,
  which is why the dry run never saw it.
- GitHub publishes a job's log a little after the run completes. Observing in that window gave only a
  `log_unavailable` infrastructure finding and a genuine source failure was re-dispatched instead of
  repaired. A failed run without a readable log is now observed again for up to ten minutes.
- This organisation hands no GitHub-hosted runners to the scratch repository: jobs on `ubuntu-latest`
  and `ubuntu-24.04` queued indefinitely, private or public. TauCeti's own jobs run on Namespace, and
  the scaffold's jobs use `nscloud-ubuntu-24.04-amd64-2x4`, which starts within seconds.
- The installation's permissions lag the App registration until the operator accepts the update:
  `checks: write` was registered but the token carried `checks: read` for most of the run. The rig
  reads the token's permissions, not the registration, and skips the eligibility scenario with that
  reason; re-run `--only clean` once the token carries `write`.
- The App's webhook delivery log answers 404 while the App has no active webhook, so deliveries are
  synthesized from the API objects the rehearsal creates and the service observes CI by polling.
- The operator's token is a shared account quota: approval comments, admin calls and every `gh`
  call the operator makes during the run draw on it, and the service resolves the bearer token
  through `GET /user` on each fresh process. Keep `gh` use during a run to a minimum; a restart no
  longer re-applies settings for that reason.
- GitHub's own degradation (5xx on pushes and PR creation, cancellations not processed for an hour,
  `force-cancel` needed to free a concurrency group) is survivable: pushes retry, a PR create that
  was applied but answered 5xx is adopted on the retry, and the waits are hours rather than minutes.
- An infrastructure re-dispatch creates a second `pr-build.yml` run with the same `run-name` (same
  head, base and batch id). The adapter raised `AmbiguousCandidateRun` for two runs with one title,
  so a head whose first run was cancelled, lost or reported without its status could never finish;
  the fake picked the newest run, so the fixture did not see it. Fixed in this stream: the controller
  passes the attempt's dispatch time as `since`, and the adapter answers with the newest run created
  at or after it (`docs/queue/github.md`).
- The service's `review_app_id` was fixed at TauCetiReview's App id; it is now a configuration key so
  the scratch repository can be approved by checks the queue App publishes.

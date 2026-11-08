# Useful local checks, then self-tested PRs

Implement this in two stages. First make the standard local commands run the checks authors need. Then let trusted authors skip expensive PR CI, while keeping review and staging tests.

## Stage 1 — Improve local behaviour; keep PR CI unchanged

The normal local sequence becomes:

```bash
lake exe cache get && lake cache get && lake build --iofail && lake test && lake lint
```

The cache commands retain their existing behaviour. TauCeti already sets `warningAsError`; Lake currently requires `--iofail` on the command line. No Lean/Lake patch is part of this work.

### Make the commands do useful work

| Command | Behaviour after this stage |
| --- | --- |
| `lake build --iofail` | Build the whole library, including affected importers, and reject informational diagnostics. This is already the worker's required final build. |
| `lake test` | Run the existing inexpensive local axiom checks on changed modules, plus the existing module-system audit. Today no test driver is configured. |
| `lake lint` | Run the existing source style and changed-module environment/documentation/`nolint` checks, plus the cheap CI source guards listed below and applicable shim checks. Today no lint driver is configured. |
| `tauceti verify` | Run these same checks through its existing orchestration and reuse machinery. Replace its separate command recipes rather than running both. |

Use the existing local scope calculation, including uncommitted and newly created files. Keep local scope discovery offline. Preserve its handling of missing history and broad changes; do not build a new scope system.

Local axiom checks remain direct checks of changed modules' artifacts. Staging retains the full ownership and transitive axiom audits. Local verification does not need to import the whole library environment in every worker round.

### Share the code and add the missing cheap checks

1. Move the reusable local checker implementations into TauCetiTools and make Lake and Worker entry points call them. Use the same implementations and configuration as CI wherever coverage is the same. Keep the deliberate cheap-local/full-CI distinction explicit.
2. Align the Tools versions used by the affected checkout launchers, worker and workflows through their existing pin/materialization mechanism. Do not introduce a new version-management system. CI continues using its trusted tool snapshot.
3. Add the existing CI source guards to local lint: forbidden options/metaprogramming, import boundaries/headers, source shape and new-file length. Reuse the existing shim registry/expiry checks. Keep GitHub-only metadata policy in CI/admission rather than adding GitHub calls to local lint.
4. In the worker's existing verifier, run the cheap source checks before the expensive build, and artifact-dependent checks afterwards. Do not ask authors to run an extra preliminary command.
5. Update authoring, fix, rebase and bump prompts to use the common entry points. Remove the replaced shell recipes so successful checks are not repeated.

Preserve existing receipts, logs and phase reuse. No receipt-format expansion, new source fingerprinting, final-commit binding step, receipt upload, assertion service or additional verification pass is required for this stage. Fix integration problems encountered while wiring the shared commands; do not redesign verification recovery.

### Deliver and check

The changes belong in TauCetiTools (shared checkers), TauCeti (Lake drivers and tool pins), and TauCetiWorker (shared-command wiring and prompts). Land the Tools support before updating its consumers.

Extend the existing tests for the changed entry points and failure propagation. Smoke-test the real Lake commands in a small checkout and the worker wrapper in its supported environments. Confirm a representative source/lint failure is reported and successful work is not run twice. Use normal repository checks; no separate certification suite or benchmark campaign.

Deploy worker changes live with normal admissions enabled, and check useful completions and any interrupted work. Stage 1 is done when the commands above work and workers use the same local checks. PR admission and staging coverage have not changed.

## Stage 2 — Introduce self-tested and skip redundant PR CI

### The author-facing behaviour

An author runs the Stage 1 checks and applies `self-tested`. The label is their assertion that the current PR head passed. There is no required JSON attachment, separate assertion command or receipt-publication workflow. Workers can add the label after their normal successful verification; existing logs/receipts remain available for diagnosis.

Accept the label only when:

- The PR author belongs to a designated GitHub team, initially **Kim, David, Chris and the ai-for-science account**. Resolve the exact logins and team slug when implementing it. The team is the allowlist.
- The diff is entirely under `TauCeti/`, with unchanged pins and lakefile. Keep explicit `full-lint` and lint-repair requests on their existing CI route.
- The existing required reviews approve the current head, and the PR satisfies the existing admission rules.

A push clears `self-tested` automatically; it can be reapplied after testing the new head. Main advancing does not clear it. Check the current head when acting on label/push events, using the existing reconciliation path for missed events. Already accepted staging members keep their existing immutable membership rules.

A nonmember author's label has no effect, even if a member or bot applied it. Use the existing GitHub observation path for team membership. If membership is unavailable, use ordinary CI rather than introducing a new blocked state. Applying the label is sufficient author attestation; do not add another approval or authentication protocol.

### Change the whole path that currently waits for PR CI

1. **Scheduling:** decide whether the label applies before expensive runner selection and cache preparation. Keep the cheap existing scope checks, but skip the expensive ordinary PR build for eligible labelled PRs.
2. **Review and admission:** accept the eligible label in place of green ordinary PR `build`. Preserve current-head review approval and existing scope/path policy. Update review scheduling and worker selection too, so they do not wait for, redispatch or try to repair the deliberately skipped build.
3. **Existing queued work:** cancel redundant ordinary PR builds for eligible opted-in heads where practical. Handle labels added after opening a PR and suppress redundant ordinary builds for accepted staging members. Leave staging and unrelated runs alone.
4. **Reporting:** say “PR CI skipped: self-tested.” Do not post a fictional successful build or require authors to submit further evidence.
5. **Staging:** keep all current required candidate tests, repair, verdict and cache-publication behaviour. The exception applies only to admission into mandatory staging, never to direct merging.

Removing the label before admission, pushing an untested head, or leaving the allowed scope returns the PR to ordinary CI. Membership changes are handled by the same eligibility reconciliation. No extra author action is required beyond testing and applying the label.

### Deliver and check

Update the PR workflow, shared review/admission policy, worker/backstop callers and existing reconciliation paths together. The current policy requires green `build` and `scope`; skipping a workflow without changing its callers would strand PRs. Retain the cheap scope decision independently of the expensive build.

Test the changed behaviour in the existing suites: an allowed reviewed PR skips PR CI and enters staging; a nonmember or out-of-scope PR uses normal CI; a push clears the label; staging still runs and repairs a failing contribution. Cover event races where they affect those behaviours, without adding a separate verification framework.

Enable the route for the initial team once its consumers are deployed. Use a simple runtime switch to restore normal PR CI if needed; accepted staging work continues. Deploy live without draining or pausing admissions.

Use existing telemetry to compare PR runner time saved and additional staging repair/rebuild time, along with landed PRs per staging hour. These observations guide adjustments; they are not new admission conditions or prerequisites for every rollout.

Stage 2 is done when an eligible author can test, label, obtain the existing review approval and reach staging without an expensive PR build, and staging remains responsible for testing the integrated result.

## Context retained for implementation

Ordinary TauCeti PR CI currently tests an integration with captured main, following [#17052](https://github.com/TauCetiProject/TauCeti/pull/17052). This plan adds no local synthetic integration or requirement to follow each main advance. Local checks test the presented sources; staging tests integration. Changing the ordinary PR integration policy is separate work.

Staging's axiom/ownership audit is global, but its environment lint is not always whole-library: batch-changed declarations, downstream `unusedArguments` and global documentation checks are supplemented by the separate full-lint workflow. This plan does not expand staging lint coverage.

Implementation references: [worker verification](../verification.md), [current local recipes](../../prompts/local-verification.md), [CI build body](https://github.com/TauCetiProject/TauCetiTools/blob/d288926c619a7247c92a1000d44ff9157220ff54/tauceti_tools/assets/scripts/sandbox-build.sh), and [shared admission policy](https://github.com/TauCetiProject/TauCetiReview/blob/f2c57dda135775ec0117a6ae18ae9443f3e56601/runner/admission_policy.py). Use current deployed revisions when implementing.

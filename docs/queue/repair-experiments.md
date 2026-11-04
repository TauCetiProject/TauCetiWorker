# Historical repair experiments

Increment 0 of the [AI merge queue plan](../redesign/specs/ai-merge-queue.md) rests on one untested
claim: an agent given a failed staging candidate with complete diagnostics and warm build artifacts
produces a green replacement chain, and does so faster than the author round-trip. This document
records the experiment that tests the claim on the failed candidates of 2026-11-02 to 2026-11-04,
run with `tauceti_queue/repair_experiment.py`. The raw per-candidate records are in
[repair-experiments.json](repair-experiments.json); transcripts, patches, build logs and prompts stay
on the host under `~/tmp/repair-experiments/<candidate>/` and are not committed.

## Method

**Candidates.** Two snapshots of the Bors candidate journal
(`/repositories/1/candidates?base=main`, ids 1833 to 1940, taken 2026-11-03 22:51 UTC and 2026-11-04
04:16 UTC) were merged. A candidate is eligible when `bors.trusted_failure` classified it as a
`candidate_failure` and `bors.terminal_logs failure_diagnostics` recorded at least one
`compiler_error`, `duplicate_declaration` or `environment_linter` observation. Construction
conflicts, infrastructure failures, cancellations and promoted candidates are excluded. That gives 22
candidates in 13 failure lineages (lineage = the oldest ancestor reachable through the members'
`parent_candidate_id`). Candidate 1930, whose duplicate-declaration audit finding the parser missed
(staging-triage.md, change 4), was added by hand with its diagnostics taken from the CI log, for 23.

**Sources.** The retained `staging-candidate-N-G` branch is fetched into a private ref. Bors's retained
commit is already one squash commit per member on `base_sha`. The attributed chain is rebuilt with
stream A's `tauceti_queue.chain.build` from the members' accepted heads (fetched by SHA), using
Bors's squash subjects and bodies as titles and descriptions, so each commit carries
`Source-PR:` and `Accepted-Head:` trailers. The tip tree is compared with the retained tree; when it
differs the retained chain is reworded with the trailers instead and the difference recorded. The
chain is checked out in a private detached worktree of the fleet host's `~/TauCeti` clone.

**Diagnostics.** The journal's observations name the failing module paths and declarations and Bors's
attributed suspects, but not the error lines. Those come from the `sandboxed-build` job log the
journal binds (`gh api .../actions/jobs/<id>/logs`): compiler errors with their continuation lines,
`duplicate-declarations:` and `audit-driver: import ... failed` lines, and the `LINT-ENV-LOCATIONS`
findings with the lint report excerpt. File-level owners are computed from the chain (which
constituent commits touch each failing path) and given to the agent next to Bors's suspects.

**Warm artifacts.** The plan's default source: Lake's artifact cache for the candidate's `base_sha`
(`lake cache get --service tauceti-public --repo TauCetiProject/TauCeti --rev=<base>` into a private
artifact pool seeded by hardlinks from `~/.cache/lake`), then `lake build --iofail` of the whole
library at the chain tip, which restores every unchanged module and builds the batch's own modules
and their dependents, failing on the modules CI failed on. All 23 candidates pin the same Mathlib
revision (`1b1ccf4b08`) and toolchain (`leanprover/lean4:v4.36.0-rc2`), so one extracted Mathlib
(`lake exe cache get`, 7.7 GB) is shared read-only through a `.lake/packages` symlink, standing in
for the workers' shared Mathlib view. Whether the failed candidate's own outputs were published is
probed with `lake cache get --rev=<retained commit> --mappings-only`.

**Agent.** The worker's own `host_agent_argv` with the repository's Claude authoring default
(`claude-opus-5-5`, effort `high`): `claude -p <prompt> --output-format stream-json --verbose --model
claude-opus-5-5 --effort high --dangerously-skip-permissions`, `ANTHROPIC_API_KEY` unset so the Max
subscription is billed, an isolated `CLAUDE_CONFIG_DIR` seeded the way workers seed theirs
(credentials, the worker settings, no personal `CLAUDE.md`), the worker's ten-minute shell limit, a
three-hour cap, `GH_CONFIG_DIR` pointed at an empty directory and `remote.origin.pushurl` overridden
to a non-existent path so nothing can be published. The prompt opens with the plan's instruction
verbatim, then the batch, the CI evidence with owners, the constituent commits with their PR
identities, a recipe for editing constituent commits, and how to verify (`lake build --iofail`,
`scripts/lint-env.sh` for lint findings). One attempt per candidate.

**Measurement.** Wall time from handoff to the agent's exit; `chain.validate` and `chain.compare`
(replay-based provenance) on the returned chain beside a patch-id comparison; append-only message
preservation and the appended explanations; shrink events (a commit that became empty, lost whole
files, or lost more than half of its insertions, at least twenty lines); a confirming
`lake build --iofail` of the whole library, plus `scripts/lint-env.sh` for lint failures and a
textual duplicate-name check for audit failures (the audit driver itself is not re-run locally);
the full patch; tokens from the stream-json transcript counted once per message id and priced at API
rates with `tauceti_worker.cost_model` (`prices.json`), with Claude's own `total_cost_usd` beside it.
Host load (1-minute average) and free disk are recorded at every phase.

## Host

A fleet host running worker agents: 96 CPUs, 125 GB RAM, a 3.5 TB array with 25 to 66 GB free during
the runs (other processes create and delete tens of GB), 1-minute load average between 95 and 165
throughout. Experiments ran at most two at a time, grouped by base commit so that candidates sharing
a base reuse one worktree. Every timing below was taken under that contention and is an upper bound
on what a dedicated host would show; the hosted CI runner built candidate 1931's 1,189 modules in
8 minutes (22:06:18 to the first error at 22:14:18, after its own cache restore).

## Results

One row per candidate. "prep" is the base cache fetch plus the incremental `lake build --iofail`
that warms the batch's modules (and reproduces the failure); "cache warmth" counts the modules that
build had to compile on top of main's restored outputs; "agent" is wall time from handoff to exit;
"green" is the harness's own confirming build (plus lint or the duplicate check where relevant);
tokens and USD are from the transcript at API list prices.

<!-- results-table -->
9 candidates in 5 lineages; 8 complete, 8 green. Total 9,465,445 tokens, estimated USD 6.47 at API list prices; 0 shrink events; messages preserved as prefixes everywhere: yes.

| candidate | lineage | members | kind | failing modules | prep (min) | cache warmth | agent (min) | green | edited PRs | shrink | tokens | est. USD |
| ---: | ---: | ---: | --- | --- | ---: | --- | ---: | --- | --- | --- | ---: | ---: |
| 1843 | 1843 | 2 | compiler | Substitution | 10.4 | 214 built | 2.9 | yes | #25596 | - | 481202 | 0.54 |
| 1862 | 1861 | 56 | compiler | LDU | 12.5 | 2810 built | 1.1 | yes | #26251 | - | 673922 | 0.58 |
| 1863 | 1861 | 55 | lint | Slope | 0.2 | 0 built | - | - | - | - | - | - |
| 1914 | 1861 | 14 | lint | Slope | 15.0 | 1468 built | 13.2 | yes | #26212 | - | 945515 | 0.74 |
| 1916 | 1867 | 111 | compiler | Diagonalization | 2.6 | 435 built | 3.1 | yes | #25202, #25416, #26269 | - | 1119556 | 0.82 |
| 1918 | 1873 | 56 | compiler | Braiding | 11.7 | 4077 built | 45.2 | yes | #23638, #24902, #25391, #25667 | - | 4366931 | 2.17 |
| 1930 | 1886 | 116 | audit | Menger, Paths | 14.9 | 4903 built | 1.0 | yes | #24945 | - | 336391 | 0.40 |
| 1931 | 1867 | 110 | compiler | Pullback | 10.7 | 4645 built | 4.7 | yes | #25202, #26269 | - | 1120952 | 0.78 |
| 1932 | 1867 | 109 | compiler | CompletedModules | 0.4 | 0 built | 1.1 | yes | #25202 | - | 420976 | 0.42 |

Minutes:

- agent wall time, handoff to exit (n=8): min 1.0, p25 1.1, median 3.1, p75 4.7, max 45.2, mean 9.0
- time to green (agent plus confirming build) (n=8): min 1.1, p25 1.3, median 3.3, p75 4.9, max 45.3, mean 9.2
- preparation (base cache fetch plus incremental build) (n=9): min 0.2, p25 2.6, median 10.7, p75 12.5, max 15.0, mean 8.7
- modules built in preparation (n=9): min 0, p25 214, median 1468, p75 4077, max 4903, mean 2061.3
<!-- /results-table -->

## Warm-artifact decision

_Pending the sample._

## Limitations

- Host contention: load averages above 100 throughout; timings are upper bounds.
- One attempt per candidate; the agent's variance is not measured.
- Subscription billing: costs are estimates at API list prices from the transcript's token counts,
  not invoices.
- Candidates in one lineage differ by one removed PR, so later candidates on a shared base find more
  of their modules already built in the pool; the first candidate on each base is the clean
  measurement of main's cache plus an incremental build.
- The lint and audit confirmations are partial: `scripts/lint-env.sh` runs for lint failures, scoped to
  the batch's changed modules as CI scopes it (candidate 1914's first confirming run was unscoped and
  reported a `simpNF` violation already on `main` in a module the batch does not touch; its row is
  judged on the build and the agent's scoped lint run), and the duplicate-declaration audit driver is
  replaced by a textual check.
- Disk, not time or inference, bounded the run. Restoring `main`'s cache at a base unpacks 14 GB into
  the worktree, and Lake stores every artifact a build produces into the pool (hardlinked with the
  worktree), about 13 GB for a 110-PR candidate, so one base group is live at a time and the pool is
  pruned to the published mappings whenever a group ends. The host's free space moved between 14 GB
  and 103 GB during the runs as other processes came and went; at 07:40 UTC it was 17 GB with 23 GB
  of the pool belonging to the running group. From then on the lane required 25 GB free before and
  after preparation (`--min-free-gb 25`) and stopped when the host could not provide it, which is why
  the sample may be shorter than the 23 eligible candidates.

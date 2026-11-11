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

A fleet host running worker agents: 96 CPUs, 125 GB RAM, a 3.5 TB array whose free space moved
between 0 and 113 GB during the runs as other processes came and went, 1-minute load average between
3 and 229 (the load at each agent handoff is in the JSON). Experiments ran one base commit at a time,
candidates sharing a base reusing one worktree. Every timing below was taken under that contention
and is an upper bound on what a dedicated host would show: the hosted CI runner built candidate
1931's 1,189 modules in 8 minutes (22:06:18 to the first error at 22:14:18, after its own cache
restore), this host built its 4,645 in 10.6 minutes at load 127.

## Results

One row per candidate. "prep" is the base cache fetch plus the incremental `lake build --iofail`
that warms the batch's modules (and reproduces the failure); "cache warmth" counts the modules that
build had to compile on top of main's restored outputs; "agent" is wall time from handoff to exit;
"green" is the harness's own confirming build (plus lint or the duplicate check where relevant);
tokens and USD are from the transcript at API list prices.

<!-- results-table -->
10 candidates in 5 lineages; 9 complete, 9 green. Total 11,036,636 tokens, estimated USD 7.48 at API list prices; 0 shrink events; messages preserved as prefixes everywhere: yes.

| candidate | lineage | members | kind | failing modules | prep (min) | cache warmth | agent (min) | green | edited PRs | shrink | tokens | est. USD |
| ---: | ---: | ---: | --- | --- | ---: | --- | ---: | --- | --- | --- | ---: | ---: |
| 1843 | 1843 | 2 | compiler | Substitution | 10.4 | 214 built | 2.9 | yes | #25596 | - | 481202 | 0.54 |
| 1862 | 1861 | 56 | compiler | LDU | 12.5 | 2810 built | 1.1 | yes | #26251 | - | 673922 | 0.58 |
| 1863 | 1861 | 55 | lint | Slope | 0.2 | 0 built | 43.1 | yes | #26212 | - | 1571191 | 1.02 |
| 1864 | 1861 | 27 | lint | Slope | 1.6 | 270 built | - | aborted | - | - | - | - |
| 1914 | 1861 | 14 | lint | Slope | 15.0 | 1468 built | 13.2 | yes | #26212 | - | 945515 | 0.74 |
| 1916 | 1867 | 111 | compiler | Diagonalization | 2.6 | 435 built | 3.1 | yes | #25202, #25416, #26269 | - | 1119556 | 0.82 |
| 1918 | 1873 | 56 | compiler | Braiding | 11.7 | 4077 built | 45.2 | yes | #23638, #24902, #25391, #25667 | - | 4366931 | 2.17 |
| 1930 | 1886 | 116 | audit | Menger, Paths | 14.9 | 4903 built | 1.0 | yes | #24945 | - | 336391 | 0.40 |
| 1931 | 1867 | 110 | compiler | Pullback | 10.7 | 4645 built | 4.7 | yes | #25202, #26269 | - | 1120952 | 0.78 |
| 1932 | 1867 | 109 | compiler | CompletedModules | 0.4 | 0 built | 1.1 | yes | #25202 | - | 420976 | 0.42 |

Minutes:

- agent wall time, handoff to exit (n=9): min 1.0, p25 1.1, median 3.1, p75 13.2, max 45.2, mean 12.8
- time to green (agent plus confirming build) (n=9): min 1.1, p25 1.3, median 3.3, p75 13.3, max 45.3, mean 13.0
- preparation (base cache fetch plus incremental build) (n=10): min 0.2, p25 1.6, median 10.4, p75 12.5, max 15.0, mean 8.0
- modules built in preparation (n=10): min 0, p25 214, median 435, p75 4077, max 4903, mean 1882.2
<!-- /results-table -->

Nine candidates in five lineages ran to completion before the host's disk forced the stop
(candidate 1864 was aborted during its agent run and is recorded as such; thirteen eligible
candidates were not started, see [Resuming](#resuming)). Every completed candidate ended green: a
confirming whole-library `lake build --iofail`, plus the CI-scoped environment lint for the two lint
candidates and the duplicate-name check for the audit candidate. All edits landed in the owning
constituent commits with the original messages preserved as prefixes and a one-line "Batch repair:"
explanation appended; no contribution was shrunk or emptied; `chain.validate` and `chain.compare`
accepted every returned chain. The chain tool rebuilt Bors's retained tree exactly from the accepted
heads for all ten candidates.

**Time to green.** Agent wall time from handoff to exit was 1.0, 1.1, 1.1, 2.9, 3.1, 4.7, 13.2, 43.1
and 45.2 minutes (median 3.1, mean 12.8); the confirming build added 6 to 16 seconds each time,
because the agent's own final `lake build --iofail` had already built everything, so time to green is
1.1 to 45.3 minutes with median 3.3 and p75 13.3. For comparison, the staging triage deployment saw
19 to 39 minutes from handoff to a repaired head for the author round-trip, followed by hours behind
the backlog. Six of nine repairs took under five minutes. The three long ones are instructive:

- 1918 (56 members, 45 minutes, 56 turns, USD 2.17): four independently broken modules in four PRs,
  only one of which CI had reported (fail-fast), including a `linftyOne` rename on `main` that the
  batch's PR had missed.
- 1863 and 1914 (lint lineage, 43 and 13 minutes): the real finding (an unused `[Module ℤ M]`
  binder in `Slope.lean`) was fixed in a few minutes each time; the rest went into whole-library lint
  runs that hit the worker's ten-minute shell limit, then into a `simpNF` finding already on `main`
  in a module the batch does not touch, which an unscoped `lint-env.sh` reports and CI's scoped run
  does not. The repair task should hand the agent the batch's changed-module list for the lint,
  as CI uses it, and a shell limit long enough for one scoped run.

**One repair per lineage, not per round.** Because the local build runs to completion, the agent
sees every independently broken module at once: 1916 repaired three PRs (#25416, #25202, #26269)
in 3.1 minutes, where Bors needed three rounds (1916, 1931, 1932) of about 30 minutes each to
remove them one at a time; 1931's agent likewise fixed #25202 before Bors found it in 1932. The
descendants' own experiments then had little left to do (1.1 minutes each).

**Cost.** 11.0 million tokens over nine runs (97% prompt-cache reads), USD 7.48 at API list prices
(Claude's own `total_cost_usd` agrees to the cent on every run), median USD 0.74 per repair, maximum
USD 2.17.

**Explanations after trailers.** Following the instruction literally, agents appended their
explanation after the `Source-PR`/`Accepted-Head` block, so git's own `interpret-trailers` no longer
sees those trailers on edited commits. `tauceti_queue.chain` reads trailers on any line and was
unaffected; anything else that parses the trailers (comment generation, retained-ref naming) must do
the same, or the instruction must say to insert the explanation before the trailer block.

## Warm-artifact decision

`main`'s published cache at the base plus an incremental build of the batch's modules is warm
enough, and it is the only source that exists for a failed candidate: the staging workflow stages
and publishes a candidate's outputs only after every audit passes (`STAGE_LAKE_CACHE`), so
`lake cache get --rev=<retained commit>` found no outputs for any of the ten failed candidates
(`candidate_outputs_published` is false in every record). The evidence for "warm enough":

| candidate | base reused | base cache fetch | modules unpacked from the base mapping | modules built | incremental build | confirming build after repair |
| ---: | --- | ---: | ---: | ---: | ---: | ---: |
| 1843 | no | 5 s (1,092 artifacts) | 42,016 | 214 | 10.3 min | 9 s |
| 1862 | no | 4 s | 20,804 | 2,810 | 12.4 min | 6 s |
| 1914 | no | 6 s | 22,381 | 1,468 | 14.9 min | 11 s |
| 1931 | yes (seed) | 7 s | 0 | 4,645 | 10.6 min | 16 s |
| 1930 | yes | 5 s | 2,536 | 4,903 | 14.8 min | 7 s |
| 1918 | yes | 5 s | 2,715 | 4,077 | 11.6 min | 6 s |
| 1916 | yes | 5 s | 894 | 435 | 2.5 min | 11 s |
| 1932 | yes | 7 s | 1 | 0 | 16 s | 13 s |
| 1863 | yes | 3 s | 0 | 0 | 6 s | 15 s |

The base mapping was found for every base and its artifacts were almost all already present (the
pool was seeded from the host's `~/.cache/lake`); fetching cost seconds. Preparation is then the
incremental build: 10 to 15 minutes on this host for the first candidate on a base, dominated by
unpacking 20,000 to 42,000 restored modules and compiling the 200 to 4,900 modules the batch
changes or depends on (CI with `--fail-fast` built fewer, 7 to 2,594, because it stopped at the
first failure; the complete build is what gives the agent every culprit at once). A second candidate
on the same base, with the previous build's outputs in the pool, prepared in 6 seconds to 2.5
minutes. In the live queue a repair round's successor shares its base, so the repair worker should
keep its pool and worktree between rounds of one head and expect seconds, not minutes, after the
first round. The failed candidate's own outputs would have saved at most the first round's 10 to 15
minutes, they do not exist for failed candidates, and restoring them from workflow artifacts would
have to be built; nothing here argues for it.

The cost of this source is disk, not time: a base restore unpacks 14 GB into the worktree, and Lake
stores every artifact a build produces into the pool (hardlinked with the worktree), about 13 GB for
a 110-PR candidate, so a repair worker needs roughly 30 GB per live base and must prune the pool to
the published mappings when it moves to a new base (`prune_pool`). A task whose artifacts cannot be
prepared within that budget should be reported, as the plan says, not run cold.

## Limitations

- Sample size: nine completed candidates in five lineages, against the plan's twenty across five.
  The thirteen eligible candidates not run (1835, 1837, 1838, 1839, 1847, 1851, 1852, 1854, 1895,
  1905, 1921, 1926, 1928) and the aborted 1864 cover six more bases and three more lineages; the
  harness and the lane resume from the committed state (below).
- Host contention: load averages between 3 and 229 and free disk between 0 and 113 GB; timings are
  upper bounds. The worker's ten-minute shell limit was the binding constraint in the two long lint
  runs.
- One attempt per candidate; the agent's variance is not measured.
- Subscription billing: costs are estimates at API list prices from the transcript's token counts,
  not invoices; Claude's own `total_cost_usd` matched them.
- Candidates in one lineage differ by one removed PR, so later candidates on a shared base found
  most of their modules already built in the pool; the first candidate on each base is the clean
  measurement of `main`'s cache plus an incremental build.
- The lint and audit confirmations are partial: `scripts/lint-env.sh` runs for lint failures, scoped
  to the batch's changed modules through `LINT_ONLY_MODULES` as CI scopes it (the first confirming
  runs for 1914 and 1863 were unscoped, reported a `simpNF` violation already on `main` in a module
  the batch does not touch, and were re-measured scoped: 1863's scoped run linted the same 165
  modules CI did; 1914's row rests on the agent's own scoped run, since its base worktree was gone),
  and the duplicate-declaration audit driver is replaced by a textual check.
- Disk bounded the run. Restoring `main`'s cache at a base unpacks 14 GB into the worktree, and Lake
  stores every artifact a build produces into the pool (hardlinked with the worktree), about 13 GB
  for a 110-PR candidate, so one base group was live at a time and the pool was pruned to the
  published mappings whenever a group ended. The host's free space fell to 16 GB at 07:40 UTC, to
  1 GB at 08:39 and to 12 GB at 09:11 with other processes' usage; from 07:50 the lane required
  25 GB free before and after preparation (`--min-free-gb 25`) and stopped twice on that rule, and
  at 09:11 the operator asked for the lane to stop and the footprint to be removed.

## Resuming

Everything needed to run the remaining candidates is committed or retained:

- the journal snapshots under `~/tmp/bors-journal/` (ids 1833 to 1940; `select` reads them);
- the retained candidate commits under `refs/repair-experiments/staging-candidate-N-G`, the
  attributed chains under `refs/repair-experiments/chain/...` and `refs/tauceti-queue/...`, and the
  results under `refs/repair-experiments/result/...` in `~/TauCeti` (52 refs, no disk to speak of);
- per candidate under `~/tmp/repair-experiments/<id>/`: `result.json`, `prompt.md`,
  `ci-findings.json`, the CI job log, build and lint logs, the agent's stream-json transcript and
  the patches (58 MB in total).

Removed on 2026-11-04 at 09:15 UTC and recreated by the harness on demand: the private Lake pool
`~/tmp/repair-experiments/lake-cache` (28 GB apparent, 10 GB of it hardlinks into `~/.cache/lake`;
recreate with `cp -al ~/.cache/lake/. lake-cache/`, then `lake cache get` fills each base), the
shared Mathlib seed `~/tmp/repair-experiments/seed/packages` (7.7 GB; recreate by running
`lake exe cache get` in a detached worktree of any candidate and moving its `.lake/packages`
there), and the base worktrees (14 GB each, created by `run`). On a host with about 40 GB free:

```
python -m tauceti_queue.repair_experiment select            # 22 eligible, add --include 1930 for 23
python -m tauceti_queue.repair_experiment run --candidate 1921 --min-free-gb 25
python -m tauceti_queue.repair_experiment run --candidate 1926 --remove-worktree   # last on its base
python -m tauceti_queue.repair_experiment report --json docs/queue/repair-experiments.json --doc docs/queue/repair-experiments.md
```

Candidates sharing a base should run consecutively with `--remove-worktree` on the last one
(bases: 1921 and 1926; 1851, 1852 and 1854; 1837, 1838 and 1839; 1835; 1847; 1895; 1905; 1928;
1864 with 1862 and 1863's base). `measure --candidate N` repeats a measurement from the retained
result ref.
